"""Append-only daily repo sync audit lines under LOG_DIR/repo_logs/ (IST calendar day)."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from agent.log_paths import ist_now, repo_logs_dir


def _log_dir() -> Path:
    return Path(os.getenv("LOG_DIR", "logs"))


def repo_sync_log_path_for_today() -> Path:
    d = ist_now().strftime("%Y-%m-%d")
    return repo_logs_dir(_log_dir()) / f"{d}.log"


def append_repo_sync_line(message: str) -> None:
    """Append one line with IST timestamp prefix."""
    path = repo_sync_log_path_for_today()
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = ist_now().strftime("%Y-%m-%d %H:%M:%S %Z")
    line = f"{ts} | {message}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


class RepoSyncDailyFileHandler(logging.Handler):
    """Logging handler: append to repo_logs/YYYY-MM-DD.log (IST), reopen when date rolls over."""

    def __init__(self, log_dir: Optional[str | Path] = None) -> None:
        super().__init__()
        self._base = Path(log_dir) if log_dir is not None else _log_dir()
        self._date_key: Optional[str] = None
        self._stream: Optional[object] = None

    def _ensure_stream(self) -> None:
        from agent.log_paths import ist_date_str

        today = ist_date_str()
        if self._date_key != today or self._stream is None:
            if self._stream is not None:
                self._stream.close()
            self._date_key = today
            p = repo_logs_dir(self._base) / f"{today}.log"
            self._stream = open(p, "a", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream()
            msg = self.format(record)
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        super().close()
