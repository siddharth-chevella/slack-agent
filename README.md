# OLake Slack Community Agent

An intelligent AI agent for the OLake community in Slack. It answers support questions by summarising OLake context, identifying relevant repositories, and searching the codebase (ripgrep + ast-grep) across multiple repos (olake, olake-ui, olake-docs, olake-helm, olake-fusion). Responses are routed by confidence: high confidence → answer; medium → clarification; low → escalation to the team.

## 🚀 Features

- **Context summariser**: Extracts relevant sections from ABOUT_OLAKE and picks which repos (olake, olake-docs, olake-ui, etc.) are relevant for each question.
- **Deep researcher**: Multi-iteration “Alex” agent that plans SearchParams (pattern, repo, file types), runs a single codebase search engine (ripgrep + ast-grep), and evaluates results until confident or max iterations.
- **Repo-aware search**: Single `CodebaseSearchEngine` with optional `repo` per search; deep researcher uses summariser’s `relevant_repos` for prompts and default repo when the LLM doesn’t specify one.
- **Confidence-based routing**: After research, routes to solution provider (≥0.8), clarification asker (0.5–<0.8), or escalation handler (<0.5).
- **Multiple LLM providers**: Gemini, OpenAI, OpenRouter, or Ollama (see `.env.example`).
- **Structured logging**: Events, errors, and reasoning logs under `logs/`.

## 📋 Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- Slack App with Bot Token
- LLM API: Google Gemini, OpenAI, OpenRouter, or Ollama (local)
- ngrok (for local development)

## 🛠️ Installation

1. **Install dependencies**:
   ```bash
   uv sync
   ```

2. **Configure environment**:
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

3. **Setup Slack App**:
   - Go to [api.slack.com/apps](https://api.slack.com/apps)
   - Create a new app or use existing
   - Enable **Event Subscriptions**
   - Subscribe to bot events: `message.channels`, `message.groups`, `message.im`, `app_mention`
   - Install app to workspace
   - Copy Bot Token (xoxb-...) and Signing Secret to `.env`

## 🏃 Usage

### Start the Agent

```bash
# Start webhook server
uv run python -m agent.main

# Custom port
uv run python -m agent.main --port 3000

# Validate configuration
uv run python -m agent.main --validate-config

# View statistics
uv run python -m agent.main --stats
```

### Local Development with ngrok

```bash
# Start ngrok tunnel
ngrok http 3000

# Copy the HTTPS URL (e.g., https://abc123.ngrok.io)
# Set as Request URL in Slack: https://abc123.ngrok.io/slack/events
```

### Testing

```bash
# Run the agent graph with a single message (uses get_agent_graph() like production)
uv run python test_agent.py "Your question here"

# Run unit tests
uv run pytest tests/ -v
```

## 🏗️ Architecture

```
┌─────────────────┐
│ Slack Message   │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ build_context   │ (Load thread, user; exit if org member replied)
└────────┬────────┘
         │
         ▼
┌─────────────────────────────┐
│ olake_context_summariser    │ (ABOUT_OLAKE excerpt + relevant_repos for this question)
└────────┬────────────────────┘
         │
         ▼
┌─────────────────┐
│ deep_researcher  │ (Alex: iterative SearchParams → ripgrep/ast-grep, repo-aware)
└────────┬────────┘
         │
         ▼  research_confidence
┌────────────────────────────────────────────────────────┐
│ Route: ≥0.8 → solution_provider                         │
│        0.5–<0.8 → clarification_asker                   │
│        <0.5 → escalation_handler                       │
└────────────────────────────────────────────────────────┘
```

## ⚙️ Configuration

Key environment variables (see `.env.example` for full list):

| Variable | Description |
|----------|-------------|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (xoxb-...) |
| `SLACK_SIGNING_SECRET` | For webhook verification |
| `LLM_PROVIDER` | `"gemini"`, `"openai"`, `"openrouter"`, or `"ollama"` |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` | API key for chosen provider (Ollama needs no key) |
| `OPENROUTER_MODEL` | Model when using OpenRouter (e.g. `anthropic/claude-4.6-sonnet`) |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | For local Ollama |
| `MAX_RESEARCH_ITERATIONS` | Max deep-researcher iterations (default: 5) |
| `CONFIDENCE_THRESHOLD_FOR_AUTO_REPLY` | Threshold for auto-reply (default: 0.75) |
| `LLM_REQUEST_TIMEOUT_SECONDS` | Timeout for LLM calls (default: 90) |
| `RESEARCH_TIMEOUT_SECONDS` | Timeout for full research phase (default: 120) |

## 📁 Project Structure

```
agent/
├── __init__.py
├── config.py              # Config, ABOUT_OLAKE, ABOUT_OLAKE_REPO_INFO
├── state.py               # ConversationState (incl. relevant_repos, about_olake_summary)
├── llm.py                 # get_chat_completion (Gemini/OpenAI/OpenRouter/Ollama, non-streaming)
├── codebase_search.py     # CodebaseSearchEngine, SearchParams (ripgrep + ast-grep, optional repo)
├── slack_client.py        # Slack API client
├── persistence.py         # Database layer
├── logger.py              # Structured logging
├── graph.py               # LangGraph: build_context → olake_context_summariser → deep_researcher → route
├── main.py                # Flask webhook server
├── cli_graph.py           # CLI graph (includes relevance_filter, cli solution provider)
├── cli_chat.py            # CLI chat entrypoint
└── nodes/
    ├── context_builder.py       # build_context
    ├── olake_context_summariser.py  # ABOUT_OLAKE excerpt + relevant_repos
    ├── deep_researcher.py       # Alex: SearchParams, codebase search, confidence
    ├── solution_provider.py    # Answer from research context
    ├── clarification_asker.py  # Ask user for clarification
    ├── escalation_handler.py   # Escalate to team (e.g. @mentions from olake-team.json)
    └── cli/                    # CLI-specific nodes (context, solution, relevance_filter)
```

## 📊 Logging

The agent creates structured logs in the `logs/` directory:

- `events.jsonl`: All events (messages, reasoning, responses)
- `errors.jsonl`: Error logs
- `reasoning.jsonl`: Detailed reasoning iterations
- `agent.log`: Standard log file

## 🔍 How It Works

1. **Message received**: Slack event hits the webhook.
2. **build_context**: Loads thread and user context; if an org member already replied, the bot exits silently.
3. **olake_context_summariser**: Uses the full ABOUT_OLAKE and ABOUT_OLAKE_REPO_INFO to produce a short `about_olake_summary` and a list of `relevant_repos` (e.g. olake, olake-docs, olake-fusion) for this question.
4. **deep_researcher**: “Alex” gets the summarised context and a “prefer these repos” hint. Each iteration: LLM returns SearchParams (pattern, optional repo); engine runs ripgrep + ast-grep (deduped); LLM evaluates and either continues or stops. Default repo for searches comes from `relevant_repos` when the LLM doesn’t specify one.
5. **Routing**: By `research_confidence`: ≥0.8 → **solution_provider** (answer); 0.5–<0.8 → **clarification_asker**; <0.5 → **escalation_handler** (e.g. @mentions from `olake-team.json`).

## 🎯 Future Enhancements

- Vector search for documentation (ChromaDB/Pinecone)
- GitHub issue integration
- Automated testing framework
- Analytics dashboard
- Multi-language support

## 📝 License

MIT

## 🔗 Links

- [OLake Docs](https://olake.io/docs/)
- [OLake GitHub](https://github.com/datazip-inc/olake)
- [Slack API](https://api.slack.com/)
- [LangGraph](https://python.langchain.com/docs/langgraph/)
