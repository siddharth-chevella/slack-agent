# Slack Community Agent

An AI-powered Slack bot that answers support questions by searching your codebase (ripgrep) across cloned GitHub repositories. Generic by design — configure it for any company by editing files in `config/`.

## Features

- **Deep researcher** — iterative ripgrep search loop driven by an LLM; stops when it has enough signal
- **Gate filter** — classifies every message (relevant / actionable / harmful) before any expensive work starts
- **Thread memory** — summarises conversation history so context survives across turns
- **Multiple LLM providers** — Gemini, OpenAI, OpenRouter, or Ollama
- **Dual database** — SQLite for local dev; PostgreSQL for Docker / production
- **Eval dashboard** — FastAPI UI for reviewing questions and answers

## Project Structure

```
slack-agent/
├── agent/                  # Core application
│   ├── nodes/              # LangGraph nodes (gate_filter, deep_researcher, solution_provider, …)
│   │   └── cli/            # CLI-mode variants
│   └── utils/              # JSON parser and other utilities
├── config/                 # Company-specific content — edit these to adapt the bot
│   ├── agent.yaml          # Agent name, company name, voice instruction
│   ├── about.md            # Product/company description injected into LLM prompts
│   ├── repos.md            # Codebase/repo descriptions for the researcher
│   ├── repos.yaml          # GitHub repos to clone for search
│   ├── team.json           # Org-member list (bot stays silent when a team member has replied)
│   └── terminal_allowed_commands.yaml
├── docker/                 # Dockerfiles
│   ├── Dockerfile.agent
│   └── Dockerfile.eval
├── docs/                   # Additional documentation
│   ├── QUICKSTART_CLI.md
│   └── TERMINAL_TOOL_GUIDE.md
├── eval_app/               # Evaluation dashboard (FastAPI)
├── scripts/                # Operational scripts
│   ├── start.sh            # Dev startup (clone repos, start CLI)
│   ├── sync_repos.py       # Cron-friendly repo sync
│   ├── setup_github_sync.sh
│   └── remove_github_sync.sh
├── tests/
│   └── test_agent.py       # Production-graph test harness (no real Slack)
├── docker-compose.yml      # postgres + agent + eval (optional --profile eval)
├── pyproject.toml
└── .env.example
```

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- [ripgrep](https://github.com/BurntSushi/ripgrep) (`brew install ripgrep`)
- A Slack App with a Bot Token
- An LLM API key (Gemini, OpenAI, OpenRouter) or local Ollama

## Installation

```bash
# 1. Install dependencies
uv sync

# 2. Configure environment
cp .env.example .env
# Edit .env with your Slack credentials and LLM API key

# 3. Adapt to your company (optional — works out-of-the-box for OLake)
# Edit config/agent.yaml  → agent name, company name, voice
# Edit config/about.md    → company/product description
# Edit config/repos.md    → repository descriptions
# Edit config/repos.yaml  → GitHub repos to clone
# Edit config/team.json   → team members (bot stays silent when they reply)
```

## Usage

### Webhook server (production)

```bash
uv run python -m agent.main

# With explicit port
uv run python -m agent.main --port 8001

# Validate configuration only
uv run python -m agent.main --validate-config
```

### Interactive CLI (local testing)

```bash
./scripts/start.sh        # clones repos, then starts the CLI
# or directly:
uv run python agent/cli_chat.py
```

### Local dev with ngrok

```bash
ngrok http 8001
# Set Slack Request URL to: https://<your-ngrok-id>.ngrok.io/slack/events
```

### Testing

```bash
# Run against the production graph (no real Slack)
uv run python tests/test_agent.py "How do I configure CDC with Postgres?"

# Run unit tests
uv run pytest tests/ -v
```

### Docker

```bash
# Start postgres + agent
docker compose up -d

# Also start the eval dashboard
docker compose --profile eval up -d
```

## Configuration

All runtime settings come from environment variables. See `.env.example` for the full list.

| Variable | Description |
|---|---|
| `LLM_PROVIDER` | `gemini` \| `openai` \| `openrouter` \| `ollama` |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` | API key for chosen provider |
| `OPENROUTER_MODEL` | Model when using OpenRouter |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | For local Ollama |
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (xoxb-…) |
| `SLACK_SIGNING_SECRET` | For webhook request verification |
| `DATABASE_URL` | PostgreSQL connection string (omit for SQLite) |
| `AGENT_NAME` | Override agent name from `config/agent.yaml` |
| `COMPANY_NAME` | Override company name from `config/agent.yaml` |

Required Slack bot token scopes: `chat:write`, `reactions:write`, `users:read`

## Adapting for a New Company

Edit the five files in `config/` — no Python code changes needed:

1. **`config/agent.yaml`** — set `agent_name`, `company_name`, `company_voice`
2. **`config/about.md`** — replace with your product description
3. **`config/repos.md`** — describe your repositories
4. **`config/repos.yaml`** — list your GitHub repos to clone
5. **`config/team.json`** — list team members whose replies silence the bot

## Eval Dashboard

```bash
# Locally
uv run eval-dashboard        # http://localhost:8000

# Via Docker
docker compose --profile eval up -d
```

## Links

- [LangGraph](https://python.langchain.com/docs/langgraph/)
- [Slack API](https://api.slack.com/)
- [uv](https://docs.astral.sh/uv/)
