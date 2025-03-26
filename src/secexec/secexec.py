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
            # Handle empty command case
            if not command_str.strip():
                return ("", "", 0)
                
            # Handle commands with parentheses by replacing them with bash execution
            if "(" in command_str and ")" in command_str:
                # For subshell expressions, we'll use actual bash but capture the output
                process = subprocess.Popen(
                    ['bash', '-c', command_str],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    cwd=cwd,
                    env={**os.environ, **(env or {})}
                )
                stdout, stderr = process.communicate()
                return (stdout.decode("utf-8", errors="replace"), 
                        stderr.decode("utf-8", errors="replace"), 
                        process.returncode or 0)
                
            # Special handling for common patterns
            if "&&" in command_str:
                parts = command_str.split("&&")
                all_stdout = []
                all_stderr = ""
                last_rc = 0
                
                for i, part in enumerate(parts):
                    part = part.strip()
                    if not part:
                        continue
                        
                    stdout, stderr, rc = self.execute(part, cwd, env)
                    all_stderr += stderr
                    
                    if rc == 0:
                        all_stdout.append(stdout.strip())
                    else:
                        last_rc = rc
                        break
                
                return ("\n".join(all_stdout), all_stderr, last_rc)
                
            elif "||" in command_str:
                parts = command_str.split("||")
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                        
                    stdout, stderr, rc = self.execute(part, cwd, env)
                    if rc == 0:
                        return (stdout, stderr, rc)
                
                # If we get here, all commands failed
                stdout, stderr, rc = self.execute(parts[-1].strip(), cwd, env)
                return (stdout, stderr, rc)
                
            elif ";" in command_str:
                parts = command_str.split(";")
                all_stdout = []
                all_stderr = ""
                last_rc = 0
                
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                        
                    stdout, stderr, rc = self.execute(part, cwd, env)
                    if stdout.strip():
                        all_stdout.append(stdout.strip())
                    all_stderr += stderr
                    last_rc = rc
                
                return ("\n".join(all_stdout), all_stderr, last_rc)
            
            # For standard commands, use the bashlex parser
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
                word = part.word
                
                # Basic shell variable expansion for arguments
                if '$' in word and env:
                    for var_name, var_value in env.items():
                        word = word.replace(f"${var_name}", var_value)
                        word = word.replace(f"${{{var_name}}}", var_value)
                        word = word.replace(f"${var_name}$", var_value + "$")
                        # Replace at word boundaries
                        if word == f"${var_name}":
                            word = var_value
                
                args.append(word)

        if not args:
            return 0, b"", b""

        try:
            # Create environment with provided env dict and system env
            merged_env = os.environ.copy()
            if env:
                merged_env.update(env)
                
            proc = subprocess.Popen(
                args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=merged_env,
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
                        word = part.word
                        
                        # Basic shell variable expansion for arguments
                        if '$' in word and env:
                            for var_name, var_value in env.items():
                                word = word.replace(f"${var_name}", var_value)
                                word = word.replace(f"${{{var_name}}}", var_value)
                                word = word.replace(f"${var_name}$", var_value + "$")
                                # Replace at word boundaries
                                if word == f"${var_name}":
                                    word = var_value
                        
                        cmd_args.append(word)
                parsed_commands.append(cmd_args)

        if not parsed_commands:
            return 0, b"", b""

        # Create environment with provided env dict and system env
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

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
            with open(temp_files[0], "wb") as first_out:
                first_proc = subprocess.Popen(
                    first_cmd,
                    cwd=cwd,
                    stdout=first_out,
                    stderr=subprocess.PIPE,
                    env=merged_env
                )
                _, stderr1 = first_proc.communicate()
                all_stderr += stderr1

            # Middle commands: input from previous temp file, output to next temp file
            for i in range(1, len(parsed_commands) - 1):
                cmd = parsed_commands[i]
                with open(temp_files[i - 1], "rb") as stdin_file, open(temp_files[i], "wb") as stdout_file:
                    proc = subprocess.Popen(
                        cmd,
                        cwd=cwd,
                        stdin=stdin_file,
                        stdout=stdout_file,
                        stderr=subprocess.PIPE,
                        env=merged_env,
                    )
                    _, stderr_i = proc.communicate()
                    all_stderr += stderr_i

            # Last command: input from last temp file, output to pipe
            last_cmd = parsed_commands[-1]
            with open(temp_files[-1], "rb") as last_in:
                last_proc = subprocess.Popen(
                    last_cmd,
                    cwd=cwd,
                    stdin=last_in,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=merged_env,
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
                    if os.path.exists(temp_file):
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
                # For && operator, we return right's return code and concatenate outputs
                return right_rc, left_stdout + b"\n" + right_stdout if left_stdout and right_stdout else left_stdout + right_stdout, left_stderr + right_stderr
            else:
                return left_rc, left_stdout, left_stderr

        elif operator == '||':
            # OR operator: execute right only if left failed
            if left_rc != 0:
                right_rc, right_stdout, right_stderr = self._execute_node(right_node, env, cwd)
                # For || operator, we return right's return code and append its output
                return right_rc, right_stdout, left_stderr + right_stderr
            else:
                return left_rc, left_stdout, left_stderr

        elif operator == ';':
            # SEMICOLON operator: always execute both
            right_rc, right_stdout, right_stderr = self._execute_node(right_node, env, cwd)
            # For ; operator, we return right's return code and concatenate outputs
            return right_rc, left_stdout + b"\n" + right_stdout if left_stdout and right_stdout else left_stdout + right_stdout, left_stderr + right_stderr

        else:
            return left_rc, left_stdout, left_stderr

    async def aexecute(self, command_str: str, cwd: str | None = None, env: dict[str, str] = dict()) -> tuple[str, str, int]:
        """
        Execute a shell-like command string securely using bashlex parsing asynchronously
        Returns a tuple of stdout, stderr, and exit_code
        """
        try:
            # Handle empty command case
            if not command_str.strip():
                return ("", "", 0)
                
            # Handle commands with parentheses by replacing them with bash execution
            if "(" in command_str and ")" in command_str:
                # For subshell expressions, we'll use actual bash but capture the output asynchronously
                merged_env = os.environ.copy()
                if env:
                    merged_env.update(env)
                    
                process = await asyncio.create_subprocess_exec(
                    'bash', '-c', command_str,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.DEVNULL,
                    cwd=cwd,
                    env=merged_env
                )
                stdout, stderr = await process.communicate()
                return (stdout.decode("utf-8", errors="replace"), 
                        stderr.decode("utf-8", errors="replace"), 
                        process.returncode or 0)

            # Special handling for common patterns
            if "&&" in command_str:
                parts = command_str.split("&&")
                all_stdout = []
                all_stderr = ""
                last_rc = 0
                
                for i, part in enumerate(parts):
                    part = part.strip()
                    if not part:
                        continue
                        
                    stdout, stderr, rc = await self.aexecute(part, cwd, env)
                    all_stderr += stderr
                    
                    if rc == 0:
                        all_stdout.append(stdout.strip())
                    else:
                        last_rc = rc
                        break
                
                return ("\n".join(all_stdout), all_stderr, last_rc)
                
            elif "||" in command_str:
                parts = command_str.split("||")
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                        
                    stdout, stderr, rc = await self.aexecute(part, cwd, env)
                    if rc == 0:
                        return (stdout, stderr, rc)
                
                # If we get here, all commands failed
                stdout, stderr, rc = await self.aexecute(parts[-1].strip(), cwd, env)
                return (stdout, stderr, rc)
                
            elif ";" in command_str:
                parts = command_str.split(";")
                all_stdout = []
                all_stderr = ""
                last_rc = 0
                
                for part in parts:
                    part = part.strip()
                    if not part:
                        continue
                        
                    stdout, stderr, rc = await self.aexecute(part, cwd, env)
                    if stdout.strip():
                        all_stdout.append(stdout.strip())
                    all_stderr += stderr
                    last_rc = rc
                
                return ("\n".join(all_stdout), all_stderr, last_rc)
                
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
                word = part.word
                
                # Basic shell variable expansion for arguments
                if '$' in word and env:
                    for var_name, var_value in env.items():
                        word = word.replace(f"${var_name}", var_value)
                        word = word.replace(f"${{{var_name}}}", var_value)
                        word = word.replace(f"${var_name}$", var_value + "$")
                        # Replace at word boundaries
                        if word == f"${var_name}":
                            word = var_value
                
                args.append(word)

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
                        word = part.word
                        
                        # Basic shell variable expansion for arguments
                        if '$' in word and env:
                            for var_name, var_value in env.items():
                                word = word.replace(f"${var_name}", var_value)
                                word = word.replace(f"${{{var_name}}}", var_value)
                                word = word.replace(f"${var_name}$", var_value + "$")
                                # Replace at word boundaries
                                if word == f"${var_name}":
                                    word = var_value
                        
                        cmd_args.append(word)
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
                with open(temp_files[0], "wb") as first_out:
                    first_proc = await asyncio.create_subprocess_exec(
                        *first_cmd_args,
                        cwd=cwd,
                        stdout=first_out,
                        stderr=asyncio.subprocess.PIPE,
                        env=merged_env
                    )
                    _, stderr1 = await first_proc.communicate()
                    all_stderr += stderr1
            else:
                all_stderr += f"Command not found: {first_cmd[0]}".encode()
                return 127, b"", all_stderr

            # Middle commands: input from previous temp file, output to next temp file
            for i in range(1, len(parsed_commands) - 1):
                cmd = parsed_commands[i]
                cmd_path = shutil.which(cmd[0])
                if cmd_path:
                    cmd_args = [cmd_path] + cmd[1:]
                    with open(temp_files[i - 1], "rb") as stdin_file, open(temp_files[i], "wb") as stdout_file:
                        proc = await asyncio.create_subprocess_exec(
                            *cmd_args,
                            cwd=cwd,
                            stdin=stdin_file,
                            stdout=stdout_file,
                            stderr=asyncio.subprocess.PIPE,
                            env=merged_env,
                        )
                        _, stderr_i = await proc.communicate()
                        all_stderr += stderr_i
                else:
                    all_stderr += f"Command not found: {cmd[0]}".encode()
                    return 127, b"", all_stderr

            # Last command: input from last temp file, output to pipe
            last_cmd = parsed_commands[-1]
            cmd_path = shutil.which(last_cmd[0])
            if cmd_path:
                last_cmd_args = [cmd_path] + last_cmd[1:]
                with open(temp_files[-1], "rb") as last_in:
                    last_proc = await asyncio.create_subprocess_exec(
                        *last_cmd_args,
                        cwd=cwd,
                        stdin=last_in,
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
                    if os.path.exists(temp_file):
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
                # For && operator, we return right's return code and concatenate outputs
                return right_rc, left_stdout + b"\n" + right_stdout if left_stdout and right_stdout else left_stdout + right_stdout, left_stderr + right_stderr
            else:
                return left_rc, left_stdout, left_stderr

        elif operator == '||':
            # OR operator: execute right only if left failed
            if left_rc != 0:
                right_rc, right_stdout, right_stderr = await self._aexecute_node(right_node, env, cwd)
                # For || operator, we return right's return code and append its output
                return right_rc, right_stdout, left_stderr + right_stderr
            else:
                return left_rc, left_stdout, left_stderr

        elif operator == ';':
            # SEMICOLON operator: always execute both
            right_rc, right_stdout, right_stderr = await self._aexecute_node(right_node, env, cwd)
            # For ; operator, we return right's return code and concatenate outputs
            return right_rc, left_stdout + b"\n" + right_stdout if left_stdout and right_stdout else left_stdout + right_stdout, left_stderr + right_stderr

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
