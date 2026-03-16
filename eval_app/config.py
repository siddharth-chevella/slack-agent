"""
Configuration for the Agent Evaluation Dashboard.
"""

import os
from pathlib import Path

# Resolve paths relative to project root (parent of eval_app)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

DATABASE_PATH: str = os.getenv("DATABASE_PATH", str(PROJECT_ROOT / "data" / "slack_agent.db"))
LOG_DIR: str = os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs"))
EVAL_APP_PORT: int = int(os.getenv("EVAL_APP_PORT", "8000"))
