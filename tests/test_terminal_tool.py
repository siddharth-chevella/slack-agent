"""
Tests for the terminal tool with command guardrails.
"""

import tempfile
from pathlib import Path

import pytest
import yaml

from agent.terminal_tool import CommandResult, TerminalTool, TerminalToolConfig


class TestTerminalToolConfig:
    """Tests for TerminalToolConfig."""

    def test_default_config_creation(self, tmp_path):
        """Test that default config is created if file doesn't exist."""
        config_path = tmp_path / "nonexistent.yaml"
        config = TerminalToolConfig(config_path)

        assert config_path.exists()
        assert len(config.allowed_patterns) > 0
        assert len(config.allowed_commands) > 0
        assert config.timeout == 60
        assert config.max_output_length == 10000

    def test_load_custom_config(self, tmp_path):
        """Test loading a custom configuration."""
        custom_config = {
            "allowed_patterns": ["^test\\s+"],
            "allowed_commands": ["test_cmd"],
            "timeout": 30,
            "max_output_length": 5000,
        }

        config_path = tmp_path / "custom.yaml"
        with open(config_path, "w") as f:
            yaml.dump(custom_config, f)

        config = TerminalToolConfig(config_path)

        assert config.allowed_patterns == ["^test\\s+"]
        assert config.allowed_commands == ["test_cmd"]
        assert config.timeout == 30
        assert config.max_output_length == 5000

    def test_invalid_yaml_config(self, tmp_path):
        """Test handling of invalid YAML."""
        config_path = tmp_path / "invalid.yaml"
        config_path.write_text("invalid: yaml: content: [")

        with pytest.raises(ValueError, match="Invalid YAML"):
            TerminalToolConfig(config_path)


class TestTerminalTool:
    """Tests for TerminalTool."""

    @pytest.fixture
    def temp_config(self, tmp_path):
        """Create a temporary config file."""
        config_data = {
            "allowed_patterns": [
                "^ls\\s*",
                "^echo\\s+",
                "^cat\\s+",
                "^pwd$",
            ],
            "allowed_commands": ["whoami", "date", "uname"],
            "timeout": 10,
            "max_output_length": 1000,
        }

        config_path = tmp_path / "test_config.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config_data, f)

        return TerminalToolConfig(config_path)

    @pytest.fixture
    def terminal_tool(self, temp_config):
        """Create a terminal tool with test config."""
        return TerminalTool(temp_config)

    def test_allowed_exact_command(self, terminal_tool):
        """Test that exact allowed commands pass."""
        is_allowed, reason = terminal_tool.is_command_allowed("whoami")
        assert is_allowed
        assert reason is None

    def test_allowed_pattern_command(self, terminal_tool):
        """Test that commands matching patterns pass."""
        is_allowed, reason = terminal_tool.is_command_allowed("ls -la")
        assert is_allowed
        assert reason is None

    def test_blocked_command_dangerous_pipe(self, terminal_tool):
        """Test that commands with pipes are blocked."""
        is_allowed, reason = terminal_tool.is_command_allowed("ls | grep test")
        assert not is_allowed
        assert "pipe operator" in reason

    def test_blocked_command_dangerous_semicolon(self, terminal_tool):
        """Test that commands with semicolons are blocked."""
        is_allowed, reason = terminal_tool.is_command_allowed("ls; rm -rf /")
        assert not is_allowed
        assert "command separator" in reason

    def test_blocked_command_dangerous_chaining(self, terminal_tool):
        """Test that commands with && are blocked."""
        is_allowed, reason = terminal_tool.is_command_allowed("ls && echo hi")
        assert not is_allowed
        assert "command chaining" in reason

    def test_blocked_command_dangerous_substitution(self, terminal_tool):
        """Test that commands with $() are blocked."""
        is_allowed, reason = terminal_tool.is_command_allowed("echo $(whoami)")
        assert not is_allowed
        assert "command substitution" in reason

    def test_blocked_command_dangerous_redirect(self, terminal_tool):
        """Test that commands with > are blocked."""
        is_allowed, reason = terminal_tool.is_command_allowed("echo test > file.txt")
        assert not is_allowed
        assert "redirection" in reason

    def test_blocked_unknown_command(self, terminal_tool):
        """Test that unknown commands are blocked."""
        is_allowed, reason = terminal_tool.is_command_allowed("rm -rf /")
        assert not is_allowed
        assert "not in the allowed list" in reason

    def test_empty_command(self, terminal_tool):
        """Test that empty commands are blocked."""
        is_allowed, reason = terminal_tool.is_command_allowed("")
        assert not is_allowed
        assert "Empty command" in reason

    def test_execute_allowed_command(self, terminal_tool):
        """Test executing an allowed command."""
        result = terminal_tool.execute("pwd")

        assert result.success
        assert result.return_code == 0
        assert result.error_message is None
        assert len(result.stdout) > 0

    def test_execute_blocked_command(self, terminal_tool):
        """Test executing a blocked command."""
        result = terminal_tool.execute("rm -rf /")

        assert not result.success
        assert result.return_code == -1
        assert "blocked by guardrails" in result.error_message

    def test_execute_command_with_output(self, terminal_tool):
        """Test executing a command that produces output."""
        result = terminal_tool.execute("echo hello world")

        assert result.success
        assert "hello world" in result.stdout

    def test_execute_nonexistent_command(self, terminal_tool):
        """Test executing a command that doesn't exist."""
        # First add it to allowed to test execution error
        terminal_tool.config.allowed_commands.append("nonexistent_cmd_xyz")

        result = terminal_tool.execute("nonexistent_cmd_xyz")

        assert not result.success
        assert result.return_code != 0
        # stderr contains the error for command not found
        assert "command not found" in result.stderr.lower() or "executable not found" in result.stderr.lower()

    def test_get_allowed_commands_info(self, terminal_tool):
        """Test getting allowed commands info."""
        info = terminal_tool.get_allowed_commands_info()

        assert "allowed_commands" in info
        assert "allowed_patterns" in info
        assert "timeout" in info
        assert info["timeout"] == 10

    def test_list_allowed_commands(self, terminal_tool):
        """Test listing allowed commands."""
        output = terminal_tool.list_allowed_commands()

        assert "Allowed terminal commands" in output
        assert "whoami" in output
        assert "ls" in output


class TestCommandResult:
    """Tests for CommandResult dataclass."""

    def test_successful_result(self):
        """Test creating a successful result."""
        result = CommandResult(
            success=True,
            stdout="output",
            stderr="",
            return_code=0,
            command="test",
        )

        assert result.success
        assert result.stdout == "output"
        assert result.error_message is None

    def test_failed_result(self):
        """Test creating a failed result."""
        result = CommandResult(
            success=False,
            stdout="",
            stderr="error",
            return_code=1,
            command="test",
            error_message="Something went wrong",
        )

        assert not result.success
        assert result.stderr == "error"
        assert result.error_message == "Something went wrong"


class TestSecurityGuardrails:
    """Tests for security guardrails."""

    @pytest.fixture
    def permissive_tool(self, tmp_path):
        """Create a tool with permissive config for security testing."""
        config_data = {
            "allowed_patterns": [".*"],  # Allow everything via pattern
            "allowed_commands": [],
            "timeout": 10,
            "max_output_length": 1000,
        }

        config_path = tmp_path / "permissive.yaml"
        with open(config_path, "w") as f:
            yaml.dump(config_data, f)

        config = TerminalToolConfig(config_path)
        return TerminalTool(config)

    def test_pipe_blocked_even_with_permissive_pattern(self, permissive_tool):
        """Test that dangerous patterns are blocked even if regex allows them."""
        is_allowed, reason = permissive_tool.is_command_allowed("ls | cat")
        assert not is_allowed
        assert "pipe operator" in reason

    def test_semicolon_blocked_even_with_permissive_pattern(self, permissive_tool):
        """Test that semicolons are blocked even if regex allows them."""
        is_allowed, reason = permissive_tool.is_command_allowed("ls; rm -rf /")
        assert not is_allowed
        assert "command separator" in reason

    def test_command_substitution_blocked(self, permissive_tool):
        """Test that command substitution is blocked."""
        is_allowed, reason = permissive_tool.is_command_allowed("echo `whoami`")
        assert not is_allowed
        assert "command substitution" in reason

    def test_background_execution_blocked(self, permissive_tool):
        """Test that background execution is blocked."""
        is_allowed, reason = permissive_tool.is_command_allowed("sleep 10 &")
        assert not is_allowed
        assert "background execution" in reason
