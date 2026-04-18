# Terminal Tool Guide

A secure terminal execution tool for agents with configurable command guardrails.

## Overview

The terminal tool allows agents to execute shell commands safely by only permitting commands that match configured allowed patterns or exact command names. All dangerous shell operators are blocked regardless of configuration.

## Quick Start

### 1. Enable the Terminal Tool

Add to your `.env` file:
```bash
TERMINAL_TOOL_ENABLED=true
```

### 2. Configure Allowed Commands

Edit `terminal_allowed_commands.yaml` to define which commands agents can execute:

```yaml
# Exact commands (no argument validation)
allowed_commands:
  - ls
  - pwd
  - whoami
  - date

# Regex patterns for commands with arguments
allowed_patterns:
  - "^ls\\s*"           # ls with any arguments
  - "^cat\\s+"          # cat with file arguments
  - "^grep\\s+"         # grep with pattern

# Execution settings
timeout: 60                    # Command timeout in seconds
max_output_length: 10000       # Max output characters
```

### 3. Use in Your Code

```python
from agent.terminal_tool import TerminalTool

# Initialize with default config
tool = TerminalTool()

# Execute a command
result = tool.execute("ls -la")

if result.success:
    print(result.stdout)
else:
    print(f"Error: {result.error_message}")
```

## Security Features

### Blocked Dangerous Patterns

The following are **always blocked**, even if they match allowed patterns:

| Pattern | Name | Example |
|---------|------|---------|
| `|` | Pipe operator | `ls \| grep test` |
| `;` | Command separator | `ls; rm -rf /` |
| `&&` | Command chaining | `ls && echo hi` |
| `\|\|` | Command chaining | `ls \|\| echo failed` |
| `` ` `` | Command substitution | `` echo `whoami` `` |
| `$()` | Command substitution | `echo $(whoami)` |
| `>` | Output redirection | `echo test > file` |
| `<` | Input redirection | `cat < file` |
| `>>` | Output redirection | `echo test >> file` |
| `&` | Background execution | `sleep 10 &` |

### Validation Flow

1. Check for dangerous patterns (always blocked)
2. Check if command matches exact allowed commands
3. Check if command matches allowed regex patterns
4. Execute if all checks pass

## Configuration Options

### `allowed_commands`

List of exact command names that are allowed without argument validation:

```yaml
allowed_commands:
  - ls
  - pwd
  - whoami
  - date
  - uname
```

### `allowed_patterns`

List of regex patterns for commands with arguments:

```yaml
allowed_patterns:
  - "^ls\\s*"                    # ls with any args
  - "^ls\\s+-[la]*\\s*"          # ls -l, ls -a, ls -la
  - "^cat\\s+"                   # cat with file
  - "^head\\s+-n\\s+\\d+\\s+"    # head -n 10 file
  - "^grep\\s+-[rli]*\\s+"       # grep -r, grep -l, etc.
  - "^find\\s+\\.\\s+"           # find . -name ...
  - "^tree\\s*-L\\s+\\d+"        # tree -L 2
```

### `timeout`

Maximum execution time in seconds (default: 60):

```yaml
timeout: 60
```

### `max_output_length`

Maximum output length in characters. Longer output is truncated (default: 10000):

```yaml
max_output_length: 10000
```

### `working_directory`

Optional working directory for all commands:

```yaml
working_directory: /path/to/project
```

## API Reference

### `TerminalTool`

Main class for terminal execution.

#### `__init__(config: Optional[TerminalToolConfig] = None)`

Initialize with optional custom config.

#### `execute(command: str, timeout: Optional[int] = None, working_dir: Optional[Path] = None) -> CommandResult`

Execute a command if it passes guardrails.

#### `is_command_allowed(command: str) -> tuple[bool, Optional[str]]`

Check if a command would be allowed. Returns `(is_allowed, reason)`.

#### `get_allowed_commands_info() -> dict`

Get configuration info for agent context.

#### `list_allowed_commands() -> str`

Get human-readable list of allowed commands.

### `CommandResult`

Dataclass with execution results.

```python
@dataclass
class CommandResult:
    success: bool           # True if command succeeded
    stdout: str             # Standard output
    stderr: str             # Standard error
    return_code: int        # Exit code (-1 for blocked)
    command: str            # Executed command
    error_message: Optional[str]  # Error if blocked or failed
```

## Examples

### Basic Usage

```python
from agent.terminal_tool import TerminalTool

tool = TerminalTool()

# List files
result = tool.execute("ls -la")
print(result.stdout)

# Read a file
result = tool.execute("cat README.md")
print(result.stdout)

# Search for text
result = tool.execute("grep -r 'pattern' .")
print(result.stdout)
```

### Custom Configuration

```python
from pathlib import Path
from agent.terminal_tool import TerminalTool, TerminalToolConfig

# Load custom config
config = TerminalToolConfig(Path("/path/to/custom_config.yaml"))
tool = TerminalTool(config)

# Execute with custom timeout
result = tool.execute("find . -name '*.py'", timeout=30)
```

### Check Before Execute

```python
tool = TerminalTool()

# Check if command is allowed
command = "rm -rf /"
is_allowed, reason = tool.is_command_allowed(command)

if not is_allowed:
    print(f"Command blocked: {reason}")
else:
    result = tool.execute(command)
```

## Testing

Run the test suite:

```bash
uv run pytest tests/test_terminal_tool.py -v
```

## Best Practices

1. **Minimal Permissions**: Only allow commands that are absolutely necessary
2. **Specific Patterns**: Use specific regex patterns instead of broad ones
3. **Avoid Wildcards**: Don't use `.*` patterns that allow everything
4. **Review Regularly**: Audit allowed commands periodically
5. **Use Timeout**: Set appropriate timeouts for long-running commands
6. **Limit Output**: Use `max_output_length` to prevent memory issues

## Troubleshooting

### Command is blocked but should be allowed

1. Check if the command contains dangerous patterns (pipes, redirects, etc.)
2. Verify the command matches an allowed pattern or exact command
3. Test with `is_command_allowed()` to see the specific reason

### Output is truncated

Increase `max_output_length` in the config file:

```yaml
max_output_length: 50000
```

### Command times out

Increase `timeout` in the config file:

```yaml
timeout: 120
```

## Files

- `agent/terminal_tool.py` - Main implementation
- `terminal_allowed_commands.yaml` - Default configuration
- `tests/test_terminal_tool.py` - Test suite
