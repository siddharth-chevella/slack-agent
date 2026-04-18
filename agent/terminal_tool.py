"""
Terminal tool with command guardrails for agents.

This module provides a safe terminal execution environment where agents
can only run commands that match configured allowed patterns.
"""

import logging
import re
import shlex
import subprocess
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CommandResult:
    """Result of a command execution."""
    success: bool
    stdout: str
    stderr: str
    return_code: int
    command: str
    error_message: Optional[str] = None


class TerminalToolConfig:
    """Configuration for terminal tool guardrails."""

    def __init__(self, config_path: Optional[Path] = None):
        """
        Initialize terminal tool configuration.

        Args:
            config_path: Path to the YAML configuration file.
                        Defaults to project root / terminal_allowed_commands.yaml
        """
        if config_path is None:
            config_path = Path(__file__).parent.parent / "terminal_allowed_commands.yaml"

        self.config_path = config_path
        self.allowed_patterns: list[str] = []
        self.allowed_commands: list[str] = []
        self.timeout: int = 60  # Default timeout in seconds
        self.max_output_length: int = 10000  # Max characters in output
        self.working_directory: Optional[Path] = None

        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from YAML file."""
        if not self.config_path.exists():
            # Create default config file
            self._create_default_config()
            return

        try:
            with open(self.config_path, "r") as f:
                config = yaml.safe_load(f)

            if config is None:
                self._create_default_config()
                return

            self.allowed_patterns = config.get("allowed_patterns", [])
            self.allowed_commands = config.get("allowed_commands", [])
            self.timeout = config.get("timeout", 60)
            self.max_output_length = config.get("max_output_length", 10000)

            working_dir = config.get("working_directory")
            if working_dir:
                self.working_directory = Path(working_dir)

        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in config file: {e}")

    def _create_default_config(self) -> None:
        """Create a default configuration file."""
        default_config = {
            "allowed_patterns": [
                "^ls\\s*",
                "^cat\\s+",
                "^head\\s+",
                "^tail\\s+",
                "^grep\\s+",
                "^find\\s+",
                "^pwd",
                "^echo\\s+",
                "^wc\\s+",
                "^tree\\s*",
            ],
            "allowed_commands": [
                "ls",
                "pwd",
                "whoami",
                "date",
                "uname",
            ],
            "timeout": 60,
            "max_output_length": 10000,
        }

        with open(self.config_path, "w") as f:
            yaml.dump(default_config, f, default_flow_style=False)

        self.allowed_patterns = default_config["allowed_patterns"]
        self.allowed_commands = default_config["allowed_commands"]
        self.timeout = default_config["timeout"]
        self.max_output_length = default_config["max_output_length"]


class TerminalTool:
    """
    Terminal tool with command guardrails.

    This tool allows agents to execute terminal commands safely by only
    permitting commands that match configured allowed patterns or exact commands.
    """

    def __init__(self, config: Optional[TerminalToolConfig] = None):
        """
        Initialize the terminal tool.

        Args:
            config: TerminalToolConfig instance. If None, loads default config.
        """
        self.config = config or TerminalToolConfig()

    def is_command_allowed(self, command: str) -> tuple[bool, Optional[str]]:
        """
        Check if a command is allowed based on guardrails.

        Args:
            command: The command to validate.

        Returns:
            Tuple of (is_allowed, reason). If allowed, reason is None.
        """
        command = command.strip()

        if not command:
            return False, "Empty command"

        # Defense-in-depth substring blocklist. The real safety boundary is
        # `execute()` running with shell=False + shlex.split, which prevents
        # these metacharacters from being interpreted by a shell at all.
        dangerous_patterns = [
            ("|", "pipe operator"),
            (";", "command separator"),
            ("&&", "command chaining"),
            ("||", "command chaining"),
            ("`", "command substitution"),
            ("$(", "command substitution"),
            (">", "output redirection"),
            ("<", "input redirection"),
            (">>", "output redirection"),
            ("&", "background execution"),
            ("\\n", "newline injection"),
            ("\\r", "carriage return injection"),
        ]

        for pattern, name in dangerous_patterns:
            if pattern in command:
                return False, f"Dangerous pattern detected: {name}"

        # Check against exact allowed commands (first word)
        command_parts = command.split()
        base_command = command_parts[0] if command_parts else ""

        if base_command in self.config.allowed_commands:
            return True, None

        # Check against allowed regex patterns
        for pattern in self.config.allowed_patterns:
            try:
                if re.match(pattern, command):
                    return True, None
            except re.error as e:
                logging.getLogger(__name__).warning(
                    "Invalid regex pattern %r in terminal_allowed_commands: %s", pattern, e
                )
                continue

        return False, f"Command '{base_command}' is not in the allowed list"

    def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        working_dir: Optional[Path] = None,
    ) -> CommandResult:
        """
        Execute a terminal command if it passes guardrails.

        Args:
            command: The command to execute.
            timeout: Timeout in seconds (overrides config if provided).
            working_dir: Working directory for command execution.

        Returns:
            CommandResult with execution outcome.
        """
        # Validate command
        is_allowed, reason = self.is_command_allowed(command)

        if not is_allowed:
            return CommandResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                command=command,
                error_message=f"Command blocked by guardrails: {reason}",
            )

        # Determine timeout
        exec_timeout = timeout if timeout is not None else self.config.timeout

        # Determine working directory
        work_dir = working_dir or self.config.working_directory or Path.cwd()

        try:
            # Split into argv and execute without a shell so metacharacters can't
            # be interpreted. posix=True is default on Unix; on Windows, shlex
            # still tokenizes sensibly for our simple allow-listed commands.
            try:
                argv = shlex.split(command)
            except ValueError as parse_err:
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr="",
                    return_code=-1,
                    command=command,
                    error_message=f"Command parse error: {parse_err}",
                )
            if not argv:
                return CommandResult(
                    success=False,
                    stdout="",
                    stderr="",
                    return_code=-1,
                    command=command,
                    error_message="Empty command after parsing",
                )

            result = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=exec_timeout,
                cwd=work_dir,
            )

            # Truncate output if too long
            stdout = result.stdout
            stderr = result.stderr

            if len(stdout) > self.config.max_output_length:
                stdout = (
                    stdout[: self.config.max_output_length]
                    + f"\n... [truncated, exceeded {self.config.max_output_length} chars]"
                )

            if len(stderr) > self.config.max_output_length:
                stderr = (
                    stderr[: self.config.max_output_length]
                    + f"\n... [truncated, exceeded {self.config.max_output_length} chars]"
                )

            return CommandResult(
                success=result.returncode == 0,
                stdout=stdout,
                stderr=stderr,
                return_code=result.returncode,
                command=command,
            )

        except subprocess.TimeoutExpired:
            return CommandResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                command=command,
                error_message=f"Command timed out after {exec_timeout} seconds",
            )

        except FileNotFoundError:
            return CommandResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                command=command,
                error_message=f"Command not found: {command.split()[0]}",
            )

        except Exception as e:
            return CommandResult(
                success=False,
                stdout="",
                stderr="",
                return_code=-1,
                command=command,
                error_message=f"Execution error: {str(e)}",
            )

    def get_allowed_commands_info(self) -> dict:
        """
        Get information about allowed commands for agent context.

        Returns:
            Dictionary with allowed commands and patterns.
        """
        return {
            "allowed_commands": self.config.allowed_commands,
            "allowed_patterns": self.config.allowed_patterns,
            "timeout": self.config.timeout,
            "max_output_length": self.config.max_output_length,
            "working_directory": str(self.config.working_directory)
            if self.config.working_directory
            else None,
        }

    def list_allowed_commands(self) -> str:
        """
        Get a human-readable list of allowed commands.

        Returns:
            Formatted string with allowed commands and patterns.
        """
        lines = ["Allowed terminal commands:", ""]

        if self.config.allowed_commands:
            lines.append("Exact commands:")
            for cmd in self.config.allowed_commands:
                lines.append(f"  - {cmd}")
            lines.append("")

        if self.config.allowed_patterns:
            lines.append("Command patterns (regex):")
            for pattern in self.config.allowed_patterns:
                lines.append(f"  - {pattern}")
            lines.append("")

        lines.append(f"Timeout: {self.config.timeout}s")
        lines.append(f"Max output length: {self.config.max_output_length} chars")

        if self.config.working_directory:
            lines.append(f"Working directory: {self.config.working_directory}")

        return "\n".join(lines)
