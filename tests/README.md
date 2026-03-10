# Agent tests

Tests for the OLake Slack Community Agent (codebase search, deep researcher, terminal tool, and local harness).

## Run tests

```bash
# From project root
uv run pytest tests/test_deep_researcher.py tests/test_terminal_tool.py tests/test_ripgrep_astgrep_retrieval.py -v

# Or run all collectible tests (excluding broken imports from removed services)
uv run pytest tests/ -v --ignore=tests/unit/
```

## Test modules

| Module | Description |
|--------|-------------|
| `test_deep_researcher.py` | Deep researcher node, codebase search, JSON parsing, confidence |
| `test_terminal_tool.py`   | Terminal tool config, allowed/blocked commands, security |
| `test_ripgrep_astgrep_retrieval.py` | Ripgrep/ast-grep search and research file conversion |

## Local test harness

`test_agent.py` at project root runs the full agent (Slack graph) with a patched Slack client so you can test without posting to Slack:

```bash
uv run python test_agent.py --message "Your question here"
```

## Requirements

- Python 3.11+
- Dependencies from `pyproject.toml`
- Optional: GitHub repo at `.github_repos/olake` for codebase search tests
