"""
Configuration for the Agent Evaluation Dashboard.
Reads the same env vars as the main agent so both services share one .env file.
"""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATABASE_PATH: str = os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "data" / "slack_agent.db"))
DATABASE_URL: str = (os.getenv("DATABASE_URL") or "").strip()
LOG_DIR: str = os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs"))
EVAL_APP_PORT: int = int(os.getenv("EVAL_APP_PORT", "8000"))
