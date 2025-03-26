import bashlex
import subprocess
import os
import tempfile
import logging
import asyncio
import shutil

from typing import Any

format="[%(levelname)s][%(asctime)s][%(filename)s:%(lineno)d][%(funcName)s] %(message)s"
logging.basicConfig(format=format, level=logging.INFO)
logger = logging.getLogger(__name__)


class SecExec:
    """
    A secure shell implementation that uses bashlex to parse shell syntax
    without using shell=True/create_subprocess_shell. Supports complex shell
    constructs including &&, ||, ;, |, and nested commands.
    """

    def execute(self, command_str: str, cwd: str | None = None, env: dict[str, str] = dict()) -> tuple[str, str, int]:
        """
        Execute a shell-like command string securely using bashlex parsing
        Returns CommandResult with stdout, stderr, and exit_code
        """
        try:
            # Parse the command using bashlex
            command_parts = bashlex.parse(command_str)

            all_stdout = b""
            all_stderr = b""
            last_return_code = 0

            # Execute each top-level command part
            for cmd_part in command_parts:
                rc, stdout, stderr = self._execute_node(cmd_part, env, cwd)
                all_stdout += stdout
                all_stderr += stderr
                last_return_code = rc

            return (all_stdout.decode("utf-8", errors="replace"), all_stderr.decode("utf-8", errors="replace"), last_return_code)

        except bashlex.errors.ParsingError as e:
            return ("", f"Failed to parse command: {e}", 1)
        except Exception as e:
            return ("", f"Error executing command: {e}", 1)

    def _execute_node(self, node: Any, env: dict[str, str], cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Execute a bashlex AST node based on its kind"""
        if node.kind == 'command':
            # Simple command
            return self._execute_command_node(node, env, cwd)
        elif node.kind == 'pipeline':
            # Pipeline of commands (cmd1 | cmd2 | ...)
            return self._execute_pipeline_node(node, env, cwd)
        elif node.kind == 'list':
            # List of commands (cmd1; cmd2 or cmd1 && cmd2 or cmd1 || cmd2)
            return self._execute_list_node(node, env, cwd)
        elif node.kind == 'operator':
            # Handle redirection operators (not fully implemented in this example)
            return 1, b"", "Operator node type not fully implemented".encode()
        else:
            return 1, b"", f"Unknown node type: {node.kind}".encode()

    def _execute_command_node(self, node: Any, env: dict[str, str], cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Execute a simple command node"""
        # Extract command parts (command and arguments)
        args = []
        for part in node.parts:
            if part.kind == 'word':
                # This is a command or argument
                args.append(part.word)

        if not args:
            return 0, b"", b""

        try:
            proc = subprocess.Popen(
                args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=env,
            )
            stdout, stderr = proc.communicate()
            return proc.returncode or 0, stdout, stderr

        except FileNotFoundError:
            return 127, b"", f"Command not found: {args[0]}".encode()
        except Exception as e:
            return 1, b"", str(e).encode()

    def _execute_pipeline_node(self, node: Any, env: dict[str, str], cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Execute a pipeline of commands (cmd1 | cmd2 | ...)"""
        commands = node.parts

        if len(commands) == 1:
            return self._execute_node(commands[0], env, cwd)

        # Extract commands from the pipeline
        parsed_commands = []
        for cmd in commands:
            if cmd.kind == 'command':
                cmd_args = []
                for part in cmd.parts:
                    if part.kind == 'word':
                        cmd_args.append(part.word)
                parsed_commands.append(cmd_args)

        if not parsed_commands:
            return 0, b"", b""

        # Use temporary files for the pipeline
        temp_files = []
        try:
            # Create temporary files for the pipeline
            for _ in range(len(parsed_commands) - 1):
                temp_file = tempfile.NamedTemporaryFile(delete=False)
                temp_files.append(temp_file.name)
                temp_file.close()

            all_stderr = b""

            # First command: output to first temp file
            first_cmd = parsed_commands[0]
            first_proc = subprocess.Popen(
                first_cmd,
                cwd=cwd,
                stdout=open(temp_files[0], "wb"),
                stderr=subprocess.PIPE,
                env=env
            )
            _, stderr1 = first_proc.communicate()
            all_stderr += stderr1

            # Middle commands: input from previous temp file, output to next temp file
            for i in range(1, len(parsed_commands) - 1):
                cmd = parsed_commands[i]
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdin=open(temp_files[i - 1], "rb"),
                    stdout=open(temp_files[i], "wb"),
                    stderr=subprocess.PIPE,
                    env=env,
                )
                _, stderr_i = proc.communicate()
                all_stderr += stderr_i

            # Last command: input from last temp file, output to pipe
            last_cmd = parsed_commands[-1]
            last_proc = subprocess.Popen(
                last_cmd,
                cwd=cwd,
                stdin=open(temp_files[-1], "rb"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )

            final_stdout, final_stderr = last_proc.communicate()
            all_stderr += final_stderr

            return last_proc.returncode or 0, final_stdout, all_stderr

        except Exception as e:
            return 1, b"", str(e).encode()

        finally:
            # Clean up temp files
            for temp_file in temp_files:
                try:
                    os.unlink(temp_file)
                except Exception:
                    pass

    def _execute_list_node(self, node: Any, env: dict[str, str], cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Execute a list of commands (cmd1; cmd2 or cmd1 && cmd2 or cmd1 || cmd2)"""
        if not hasattr(node, 'parts') or len(node.parts) < 3:
            return 1, b"", b"Invalid list node structure"

        left_node = node.parts[0]
        operator = node.parts[1]
        right_node = node.parts[2]

        # Execute left command
        left_rc, left_stdout, left_stderr = self._execute_node(left_node, env, cwd)

        # Check operator and decide whether to execute right command
        if operator == '&&':
            # AND operator: execute right only if left succeeded
            if left_rc == 0:
                right_rc, right_stdout, right_stderr = self._execute_node(right_node, env, cwd)
                return right_rc, left_stdout + right_stdout, left_stderr + right_stderr
            else:
                return left_rc, left_stdout, left_stderr

        elif operator == '||':
            # OR operator: execute right only if left failed
            if left_rc != 0:
                right_rc, right_stdout, right_stderr = self._execute_node(right_node, env, cwd)
                return right_rc, left_stdout + right_stdout, left_stderr + right_stderr
            else:
                return left_rc, left_stdout, left_stderr

        elif operator == ';':
            # SEMICOLON operator: always execute both
            right_rc, right_stdout, right_stderr = self._execute_node(right_node, env, cwd)
            return right_rc, left_stdout + right_stdout, left_stderr + right_stderr

        else:
            return left_rc, left_stdout, left_stderr

    async def aexecute(self, command_str: str, cwd: str | None = None, env: dict[str, str] = dict()) -> tuple[str, str, int]:
        """
        Execute a shell-like command string securely using bashlex parsing asynchronously
        Returns a tuple of stdout, stderr, and exit_code
        """
        try:
            # Parse the command using bashlex
            command_parts = bashlex.parse(command_str)

            all_stdout = b""
            all_stderr = b""
            last_return_code = 0

            # Execute each top-level command part
            for cmd_part in command_parts:
                rc, stdout, stderr = await self._aexecute_node(cmd_part, env, cwd)
                all_stdout += stdout
                all_stderr += stderr
                last_return_code = rc

            return (all_stdout.decode("utf-8", errors="replace"), all_stderr.decode("utf-8", errors="replace"), last_return_code)

        except bashlex.errors.ParsingError as e:
            return ("", f"Failed to parse command: {e}", 1)
        except Exception as e:
            return ("", f"Error executing command: {e}", 1)

    async def _aexecute_node(self, node: Any, env: dict[str, str], cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Execute a bashlex AST node based on its kind asynchronously"""
        if node.kind == 'command':
            # Simple command
            return await self._aexecute_command_node(node, env, cwd)
        elif node.kind == 'pipeline':
            # Pipeline of commands (cmd1 | cmd2 | ...)
            return await self._aexecute_pipeline_node(node, env, cwd)
        elif node.kind == 'list':
            # List of commands (cmd1; cmd2 or cmd1 && cmd2 or cmd1 || cmd2)
            return await self._aexecute_list_node(node, env, cwd)
        elif node.kind == 'operator':
            # Handle redirection operators (not fully implemented in this example)
            return 1, b"", "Operator node type not fully implemented".encode()
        else:
            return 1, b"", f"Unknown node type: {node.kind}".encode()

    async def _aexecute_command_node(self, node: Any, env: dict[str, str], cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Execute a simple command node asynchronously"""
        # Extract command parts (command and arguments)
        args = []
        for part in node.parts:
            if part.kind == 'word':
                # This is a command or argument
                args.append(part.word)

        if not args:
            return 0, b"", b""

        try:
            # Create environment with provided env dict and system env
            merged_env = os.environ.copy()
            if env:
                merged_env.update(env)

            # Resolve path for first command
            cmd_path = shutil.which(args[0])
            if not cmd_path:
                return 127, b"", f"Command not found: {args[0]}".encode()
            
            # Replace command with full path
            cmd_args = [cmd_path] + args[1:]
            
            proc = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=merged_env,
            )
            stdout, stderr = await proc.communicate()
            return proc.returncode or 0, stdout, stderr

        except FileNotFoundError:
            return 127, b"", f"Command not found: {args[0]}".encode()
        except Exception as e:
            return 1, b"", str(e).encode()

    async def _aexecute_pipeline_node(self, node: Any, env: dict[str, str], cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Execute a pipeline of commands (cmd1 | cmd2 | ...) asynchronously"""
        commands = node.parts

        if len(commands) == 1:
            return await self._aexecute_node(commands[0], env, cwd)

        # Extract commands from the pipeline
        parsed_commands = []
        for cmd in commands:
            if cmd.kind == 'command':
                cmd_args = []
                for part in cmd.parts:
                    if part.kind == 'word':
                        cmd_args.append(part.word)
                parsed_commands.append(cmd_args)

        if not parsed_commands:
            return 0, b"", b""

        # Use temporary files for the pipeline
        temp_files = []
        try:
            # Create temporary files for the pipeline
            for _ in range(len(parsed_commands) - 1):
                temp_file = tempfile.NamedTemporaryFile(delete=False)
                temp_files.append(temp_file.name)
                temp_file.close()

            all_stderr = b""
            merged_env = os.environ.copy()
            if env:
                merged_env.update(env)

            # First command: output to first temp file
            first_cmd = parsed_commands[0]
            cmd_path = shutil.which(first_cmd[0])
            if cmd_path:
                first_cmd_args = [cmd_path] + first_cmd[1:]
                first_proc = await asyncio.create_subprocess_exec(
                    *first_cmd_args,
                    cwd=cwd,
                    stdout=open(temp_files[0], "wb"),
                    stderr=asyncio.subprocess.PIPE,
                    env=merged_env
                )
                _, stderr1 = await first_proc.communicate()
                all_stderr += stderr1
            else:
                all_stderr += f"Command not found: {first_cmd[0]}".encode()

            # Middle commands: input from previous temp file, output to next temp file
            for i in range(1, len(parsed_commands) - 1):
                cmd = parsed_commands[i]
                cmd_path = shutil.which(cmd[0])
                if cmd_path:
                    cmd_args = [cmd_path] + cmd[1:]
                    proc = await asyncio.create_subprocess_exec(
                        *cmd_args,
                        cwd=cwd,
                        stdin=open(temp_files[i - 1], "rb"),
                        stdout=open(temp_files[i], "wb"),
                        stderr=asyncio.subprocess.PIPE,
                        env=merged_env,
                    )
                    _, stderr_i = await proc.communicate()
                    all_stderr += stderr_i
                else:
                    all_stderr += f"Command not found: {cmd[0]}".encode()

            # Last command: input from last temp file, output to pipe
            last_cmd = parsed_commands[-1]
            cmd_path = shutil.which(last_cmd[0])
            if cmd_path:
                last_cmd_args = [cmd_path] + last_cmd[1:]
                last_proc = await asyncio.create_subprocess_exec(
                    *last_cmd_args,
                    cwd=cwd,
                    stdin=open(temp_files[-1], "rb"),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=merged_env,
                )

                final_stdout, final_stderr = await last_proc.communicate()
                all_stderr += final_stderr
                return last_proc.returncode or 0, final_stdout, all_stderr
            else:
                return 127, b"", all_stderr + f"Command not found: {last_cmd[0]}".encode()

        except Exception as e:
            return 1, b"", str(e).encode()

        finally:
            # Clean up temp files
            for temp_file in temp_files:
                try:
                    os.unlink(temp_file)
                except Exception:
                    pass

    async def _aexecute_list_node(self, node: Any, env: dict[str, str], cwd: str | None = None) -> tuple[int, bytes, bytes]:
        """Execute a list of commands (cmd1; cmd2 or cmd1 && cmd2 or cmd1 || cmd2) asynchronously"""
        if not hasattr(node, 'parts') or len(node.parts) < 3:
            return 1, b"", b"Invalid list node structure"

        left_node = node.parts[0]
        operator = node.parts[1]
        right_node = node.parts[2]

        # Execute left command
        left_rc, left_stdout, left_stderr = await self._aexecute_node(left_node, env, cwd)

        # Check operator and decide whether to execute right command
        if operator == '&&':
            # AND operator: execute right only if left succeeded
            if left_rc == 0:
                right_rc, right_stdout, right_stderr = await self._aexecute_node(right_node, env, cwd)
                return right_rc, left_stdout + right_stdout, left_stderr + right_stderr
            else:
                return left_rc, left_stdout, left_stderr

        elif operator == '||':
            # OR operator: execute right only if left failed
            if left_rc != 0:
                right_rc, right_stdout, right_stderr = await self._aexecute_node(right_node, env, cwd)
                return right_rc, left_stdout + right_stdout, left_stderr + right_stderr
            else:
                return left_rc, left_stdout, left_stderr

        elif operator == ';':
            # SEMICOLON operator: always execute both
            right_rc, right_stdout, right_stderr = await self._aexecute_node(right_node, env, cwd)
            return right_rc, left_stdout + right_stdout, left_stderr + right_stderr

        else:
            return left_rc, left_stdout, left_stderr


s = SecExec()
print(s.execute("echo 'hi\nhello\nsyscl said hi'|grep hi|wc -l"))
print(s.execute("echo 1 && echo 2 && echo 3 && echo 4 && echo 5"))

# Async example
async def run_async_example():
    s_async = SecExec()
    result = await s_async.aexecute("echo 'async test' && ls -la | grep py")
    print("Async result:", result)

    another_cmd = "echo 'first\nsecond\nthird' | grep second|wc"
    print(s_async.execute(another_cmd))

if __name__ == "__main__":
    # Run the async example
    asyncio.run(run_async_example())
