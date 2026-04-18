# Quick Start - OLake Deep Research Agent CLI

## One Command to Start

```bash
./start.sh
```

This single command will:
1. ✅ Check all dependencies (Python, git, ripgrep, ast-grep)
2. ✅ Clone configured GitHub repositories to `.github_repos/`
3. ✅ Setup background sync (cron) for automatic updates
4. ✅ Start the interactive CLI session

## What You'll See

```
╔═══════════════════════════════════════════════════════════╗
║           OLake Deep Research Agent                       ║
║           Interactive CLI                                 ║
╚═══════════════════════════════════════════════════════════╝

Type your question below. Type 'quit' or 'exit' to end.
Type 'thinking' to see last reasoning trace.
Type 'files' to see retrieved files.

┌─
│  How does CDC work for PostgreSQL?

🧠 Thinking Process
─────────────────────────────────────────────────────────────
Okay, the user is asking about CDC for PostgreSQL. I need to
find how OLake implements pgoutput-based CDC. Let me search
for 'pgoutput' and 'CDC' in the codebase...
─────────────────────────────────────────────────────────────

📁 Retrieved Files
─────────────────────────────────────────────────────────────
1. connectors/postgres/cdc_handler.py
   Source: ripgrep | Language: python | Score: 0.90
   Why: Found while searching for: cdc postgres

🤖 Agent Response
─────────────────────────────────────────────────────────────
Based on my research in the codebase, here's what I found:

CDC (Change Data Capture) in OLake for PostgreSQL uses the
pgoutput logical decoding plugin...
─────────────────────────────────────────────────────────────

┌─
│  
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `thinking` | Show the reasoning trace from last search |
| `files` | List all retrieved files with details |
| `help` | Show available commands |
| `quit` / `exit` / `q` | End the session |

## Configuration

### Add Repositories to Track

Edit `github_repos.yaml`:

```yaml
repositories:
  olake:
    url: https://github.com/datazip-inc/olake.git
    branch: main
    enabled: true
    sync_frequency: daily
  
  your-repo:
    url: https://github.com/username/repo.git
    branch: main
    enabled: true
    sync_frequency: daily
```

### Environment Variables

Copy `.env.example` to `.env` and set your API keys:

```bash
cp .env.example .env
```

Required:
- `GEMINI_API_KEY` or `OPENAI_API_KEY` - For LLM responses

Optional:
- `MAX_RESEARCH_ITERATIONS=5` - Max research iterations
- `MAX_CONTEXT_FILES=15` - Max files to retrieve
- `MIN_CONFIDENCE_TO_STOP=0.7` - Confidence threshold

## Background Sync

The setup script will ask if you want to enable automatic repo syncing.

**Options:**
- `hourly` - Sync every hour
- `daily` - Sync daily at 3 AM (recommended)
- `weekly` - Sync every Sunday at 3 AM

**Manage sync:**
```bash
# Re-run setup
./setup_github_sync.sh daily

# Remove sync
./remove_github_sync.sh

# View logs
tail -f logs/github_sync.log
```

## Dependencies

### Required
- Python 3.11+
- git

### Recommended (for codebase search)
- ripgrep (`brew install ripgrep`)
- ast-grep (`brew install ast-grep`)

### Python Packages
Installed automatically via `uv`:
- rich (pretty CLI formatting)
- langgraph (agent workflow)
- pyyaml (config parsing)

## Troubleshooting

### "Module not found" errors
```bash
# Use uv run for proper dependency management
uv run python3 agent/cli_chat.py
```

### "git not found"
```bash
# Install git
brew install git  # macOS
sudo apt install git  # Linux
```

### "ripgrep not found" warning
```bash
# Install for fast codebase search
brew install ripgrep  # macOS
cargo install ripgrep  # Any platform
```

### Repos fail to clone
```bash
# Check github_repos.yaml syntax
cat github_repos.yaml

# Test manual clone
git clone https://github.com/datazip-inc/olake.git /tmp/test
```

## Session Behavior

- **Single conversation thread**: All messages in one session are connected
- **Context aware**: Agent remembers previous questions and answers
- **Iterative research**: Agent searches, thinks, and refines until confident
- **Pretty logging**: All input/output is formatted for readability

## Exit and Resume

```bash
# Exit session
> quit

# Start new session (conversation continues from scratch)
./start.sh
```

## Logs

- **Agent logs**: `logs/agent.log`
- **GitHub sync logs**: `logs/github_sync.log`
- **Search logs**: `logs/search.log`

```bash
# View live logs
tail -f logs/*.log
```
