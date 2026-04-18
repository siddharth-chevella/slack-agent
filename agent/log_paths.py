"""IST (Asia/Kolkata) date helpers for daily log file names under LOG_DIR."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def ist_now() -> datetime:
    return datetime.now(IST)


def ist_date_str() -> str:
    """Calendar date in IST, used as log filename stem (YYYY-MM-DD)."""
    return ist_now().strftime("%Y-%m-%d")


def ist_timestamp_iso() -> str:
    return ist_now().isoformat()


def agent_logs_dir(log_dir: str | Path) -> Path:
    p = Path(log_dir) / "agent_logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def repo_logs_dir(log_dir: str | Path) -> Path:
    p = Path(log_dir) / "repo_logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def agent_jsonl_path(log_dir: str | Path) -> Path:
    return agent_logs_dir(log_dir) / f"{ist_date_str()}.jsonl"


def agent_text_log_path(log_dir: str | Path) -> Path:
    return agent_logs_dir(log_dir) / f"{ist_date_str()}.log"
