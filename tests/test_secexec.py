import unittest
import asyncio
import subprocess
import os
import sys
from pathlib import Path

# Add the parent directory to sys.path to import secexec
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from src.secexec.secexec import SecExec


class TestSecExec(unittest.TestCase):
    def setUp(self):
        self.secexec = SecExec()
        
    def run_bash_command(self, command):
        """Run a command using actual bash and return stdout, stderr, and return code"""
        process = subprocess.Popen(
            ['bash', '-c', command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate()
        return stdout, stderr, process.returncode
    
    async def run_bash_command_async(self, command):
        """Run a command using actual bash asynchronously and return stdout, stderr, and return code"""
        process = await asyncio.create_subprocess_exec(
            'bash', '-c', command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return stdout.decode('utf-8'), stderr.decode('utf-8'), process.returncode
    
    def test_simple_command(self):
        """Test a simple command execution"""
        command = "echo 'Hello, World!'"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        self.assertEqual(secexec_result[0].strip(), bash_stdout.strip())
        self.assertEqual(secexec_result[2], bash_rc)
    
    def test_command_with_args(self):
        """Test command with arguments"""
        command = "ls -la"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        # We just check if both outputs contain some common expected strings
        # as the formatting might be slightly different
        self.assertEqual(secexec_result[2], bash_rc)
        for expected_item in ['.', '..', 'src']:
            self.assertIn(expected_item, secexec_result[0])
            self.assertIn(expected_item, bash_stdout)
    
    def test_pipeline(self):
        """Test pipeline commands (cmd1 | cmd2)"""
        command = "echo 'line1\nline2\nline3' | grep line2"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        self.assertEqual(secexec_result[0].strip(), bash_stdout.strip())
        self.assertEqual(secexec_result[2], bash_rc)
    
    def test_complex_pipeline(self):
        """Test complex pipeline with multiple commands"""
        command = "echo 'line1\nline2\nline3\nline1\nline2' | grep line | sort | uniq -c"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        self.assertEqual(secexec_result[0].strip(), bash_stdout.strip())
        self.assertEqual(secexec_result[2], bash_rc)
    
    def test_and_operator(self):
        """Test AND operator (cmd1 && cmd2)"""
        command = "echo 'first' && echo 'second'"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        # Compare outputs while ignoring whitespace differences
        self.assertEqual(self.normalize_output(secexec_result[0]), self.normalize_output(bash_stdout))
        self.assertEqual(secexec_result[2], bash_rc)
    
    def test_and_operator_fail(self):
        """Test AND operator with failing first command"""
        command = "false && echo 'should not run'"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        self.assertEqual(self.normalize_output(secexec_result[0]), self.normalize_output(bash_stdout))
        self.assertEqual(secexec_result[2], bash_rc)
        self.assertNotEqual(secexec_result[2], 0)
    
    def test_or_operator(self):
        """Test OR operator (cmd1 || cmd2)"""
        command = "false || echo 'fallback'"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        self.assertEqual(self.normalize_output(secexec_result[0]), self.normalize_output(bash_stdout))
        self.assertEqual(secexec_result[2], bash_rc)
    
    def test_semicolon_operator(self):
        """Test semicolon operator (cmd1; cmd2)"""
        command = "echo 'first'; echo 'second'"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        self.assertEqual(self.normalize_output(secexec_result[0]), self.normalize_output(bash_stdout))
        self.assertEqual(secexec_result[2], bash_rc)
    
    def test_complex_combined_operators(self):
        """Test complex commands with multiple operators"""
        command = "echo 'start' && (echo 'branch1' || echo 'fallback') && echo 'end'"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        self.assertEqual(self.normalize_output(secexec_result[0]), self.normalize_output(bash_stdout))
        self.assertEqual(secexec_result[2], bash_rc)
    
    def test_command_not_found(self):
        """Test handling of command not found"""
        command = "thiscommanddoesnotexist"
        secexec_result = self.secexec.execute(command)
        
        self.assertNotEqual(secexec_result[2], 0)
        self.assertIn("Command not found", secexec_result[1])
    
    def test_edge_case_empty_command(self):
        """Test edge case with empty command"""
        command = ""
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        self.assertEqual(secexec_result[0].strip(), bash_stdout.strip())
        self.assertEqual(secexec_result[2], bash_rc)

    def test_real_world_git_status(self):
        """Test a real-world git status command"""
        command = "git status"
        secexec_result = self.secexec.execute(command)
        bash_stdout, bash_stderr, bash_rc = self.run_bash_command(command)
        
        # Just check return code, as the output might have timing differences
        self.assertEqual(secexec_result[2], bash_rc)
        
    def test_env_variables(self):
        """Test environment variable handling"""
        env = {"TEST_VAR": "test_value"}
        command = "echo $TEST_VAR"
        secexec_result = self.secexec.execute(command, env=env)
        
        # Test the result directly rather than comparing to bash
        self.assertEqual(self.normalize_output(secexec_result[0]), "test_value")
        self.assertEqual(secexec_result[2], 0)

    def normalize_output(self, output):
        """Normalize output by stripping whitespace and joining lines for comparison"""
        if not output:
            return ""
        return " ".join([line.strip() for line in output.strip().split("\n") if line.strip()])


class TestSecExecAsync(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.secexec = SecExec()
    
    async def test_async_simple_command(self):
        """Test a simple command execution asynchronously"""
        command = "echo 'Hello, Async World!'"
        secexec_result = await self.secexec.aexecute(command)
        bash_stdout, bash_stderr, bash_rc = await self.run_bash_command_async(command)
        
        self.assertEqual(secexec_result[0].strip(), bash_stdout.strip())
        self.assertEqual(secexec_result[2], bash_rc)
    
    async def test_async_pipeline(self):
        """Test pipeline commands asynchronously"""
        command = "echo 'line1\nline2\nline3' | grep line2"
        secexec_result = await self.secexec.aexecute(command)
        bash_stdout, bash_stderr, bash_rc = await self.run_bash_command_async(command)
        
        self.assertEqual(secexec_result[0].strip(), bash_stdout.strip())
        self.assertEqual(secexec_result[2], bash_rc)
    
    async def test_async_and_operator(self):
        """Test AND operator asynchronously"""
        command = "echo 'async1' && echo 'async2'"
        secexec_result = await self.secexec.aexecute(command)
        bash_stdout, bash_stderr, bash_rc = await self.run_bash_command_async(command)
        
        self.assertEqual(self.normalize_output(secexec_result[0]), self.normalize_output(bash_stdout))
        self.assertEqual(secexec_result[2], bash_rc)
    
    async def test_async_complex_command(self):
        """Test complex command asynchronously"""
        command = "echo 'line1\nline2\nline3' | grep line | sort | uniq"
        secexec_result = await self.secexec.aexecute(command)
        bash_stdout, bash_stderr, bash_rc = await self.run_bash_command_async(command)
        
        self.assertEqual(secexec_result[0].strip(), bash_stdout.strip())
        self.assertEqual(secexec_result[2], bash_rc)
    
    def normalize_output(self, output):
        """Normalize output by stripping whitespace and joining lines for comparison"""
        if not output:
            return ""
        return " ".join([line.strip() for line in output.strip().split("\n") if line.strip()])
    
    async def run_bash_command_async(self, command):
        """Run a command using actual bash asynchronously and return stdout, stderr, and return code"""
        process = await asyncio.create_subprocess_exec(
            'bash', '-c', command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        return stdout.decode('utf-8'), stderr.decode('utf-8'), process.returncode


if __name__ == '__main__':
    unittest.main()
