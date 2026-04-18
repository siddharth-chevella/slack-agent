"""
Structured logging system for OLake Slack Community Agent.

Agent files (IST calendar day, under LOG_DIR/agent_logs/):
  - YYYY-MM-DD.jsonl — structured events, errors, node steps
  - YYYY-MM-DD.log   — slack_agent logger (DEBUG+)
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from enum import Enum

from agent.log_paths import agent_jsonl_path, agent_text_log_path, ist_timestamp_iso


class EventType(Enum):
    """Types of events to log."""
    MESSAGE_RECEIVED = "message_received"
    RESPONSE_SENT = "response_sent"
    NODE_STEP = "node_step"
    ERROR_OCCURRED = "error_occurred"


def _format_step_summary(summary: Dict[str, Any]) -> str:
    """Format a step result summary for pretty logging (one line, key=value)."""
    if not summary:
        return "(no state change)"
    parts = []
    for k, v in summary.items():
        if v is None:
            parts.append(f"{k}=None")
        elif isinstance(v, bool):
            parts.append(f"{k}={str(v).lower()}")
        elif isinstance(v, (int, float)):
            if isinstance(v, float) and 0 <= v <= 1:
                parts.append(f"{k}={v:.2f}")
            else:
                parts.append(f"{k}={v}")
        elif isinstance(v, list):
            parts.append(f"{k}=[{len(v)} items]")
        elif isinstance(v, str):
            preview = v[:50] + "…" if len(v) > 50 else v
            parts.append(f"{k}={preview!r}")
        else:
            parts.append(f"{k}={v!r}")
    return "  ".join(parts)


class _IstDailyTextFileHandler(logging.Handler):
    """Append to agent_logs/YYYY-MM-DD.log; reopen when IST date changes."""

    def __init__(self, log_dir: Path) -> None:
        super().__init__()
        self._log_dir = Path(log_dir)
        self._date_key: Optional[str] = None
        self._stream: Optional[Any] = None

    def _ensure_stream(self) -> None:
        from agent.log_paths import ist_date_str

        today = ist_date_str()
        if self._date_key != today or self._stream is None:
            if self._stream is not None:
                self._stream.close()
            self._date_key = today
            path = agent_text_log_path(self._log_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = open(path, "a", encoding="utf-8")

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


class StructuredLogger:
    """Structured logger for agent events."""

    def __init__(self, log_dir: str = "logs", log_level: str = "INFO", enable_console: bool = True):
        """
        Initialize structured logger.

        Args:
            log_dir: Base directory (LOG_DIR); writes to agent_logs/ subfolder
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
            enable_console: Whether to log to console (disable for CLI mode to avoid duplicates)
        """
        self._base_log_dir = Path(log_dir)
        self._base_log_dir.mkdir(parents=True, exist_ok=True)

        # Setup standard logger
        self.logger = logging.getLogger("slack_agent")
        self.logger.setLevel(getattr(logging, log_level.upper()))

        # Clear any existing handlers
        self.logger.handlers = []

        # Console handler with clean formatting (only if enabled)
        if enable_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_formatter = logging.Formatter(
                '%(asctime)s | %(levelname)s | %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            console_handler.setFormatter(console_formatter)
            self.logger.addHandler(console_handler)

        file_handler = _IstDailyTextFileHandler(self._base_log_dir)
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '%(asctime)s | %(name)s | %(levelname)s | %(message)s'
        )
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)

    def _write_jsonl(self, data: Dict[str, Any]) -> None:
        """Append one JSON line to today's IST agent jsonl file."""
        path = agent_jsonl_path(self._base_log_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, default=str) + "\n")

    def log_event(
        self,
        event_type: EventType,
        message: str,
        user_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log a structured event.

        Args:
            event_type: Type of event
            message: Human-readable message
            user_id: Slack user ID
            channel_id: Slack channel ID
            thread_ts: Thread timestamp
            metadata: Additional metadata
        """
        event_data = {
            "timestamp": ist_timestamp_iso(),
            "event_type": event_type.value,
            "message": message,
            "user_id": user_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "metadata": metadata or {}
        }

        self._write_jsonl(event_data)

        # Also log to standard logger
        log_msg = f"{event_type.value.upper()}: {message}"
        if user_id:
            log_msg += f" | User: {user_id}"
        if channel_id:
            log_msg += f" | Channel: {channel_id}"

        self.logger.info(log_msg)

    def log_message_received(
        self,
        user_id: str,
        channel_id: str,
        text: str,
        thread_ts: Optional[str] = None,
        user_profile: Optional[Dict[str, Any]] = None
    ) -> None:
        """Log an incoming message."""
        self.log_event(
            event_type=EventType.MESSAGE_RECEIVED,
            message=f"Message received: {text[:100]}...",
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            metadata={
                "text": text,
                "user_profile": user_profile
            }
        )

    def log_response_sent(
        self,
        channel_id: str,
        text: str,
        thread_ts: Optional[str] = None,
        *,
        source: str = "solution",
    ) -> None:
        """Log that a reply was posted to Slack (webhook path)."""
        self.log_event(
            event_type=EventType.RESPONSE_SENT,
            message=f"Slack response sent ({source})",
            user_id=None,
            channel_id=channel_id,
            thread_ts=thread_ts,
            metadata={
                "length": len(text),
                "source": source,
                "preview": text[:500],
            },
        )

    def log_error(
        self,
        error_type: str,
        error_message: str,
        stack_trace: Optional[str] = None,
        user_id: Optional[str] = None,
        channel_id: Optional[str] = None
    ) -> None:
        """Log an error."""
        error_data = {
            "timestamp": ist_timestamp_iso(),
            "error_type": error_type,
            "error_message": error_message,
            "stack_trace": stack_trace,
            "user_id": user_id,
            "channel_id": channel_id
        }

        self._write_jsonl(error_data)

        self.logger.error(
            f"ERROR: {error_type} - {error_message}",
            exc_info=stack_trace is not None
        )

    def log_step_start(self, node_name: str) -> None:
        """Log that a graph node is about to run (DEBUG on console; JSONL retained)."""
        msg = f"STEP  →  {node_name}"
        self.logger.debug(msg)
        self._write_jsonl(
            {
                "timestamp": ist_timestamp_iso(),
                "event_type": EventType.NODE_STEP.value,
                "phase": "start",
                "node": node_name,
            },
        )

    def log_step_end(
        self,
        node_name: str,
        summary: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
    ) -> None:
        """Log that a graph node finished, with optional result summary (pretty print)."""
        if error:
            msg = f"STEP  ←  {node_name}  ERROR: {error}"
        else:
            summary_str = _format_step_summary(summary or {})
            msg = f"STEP  ←  {node_name}  {summary_str}"
        self.logger.debug(msg)
        self._write_jsonl(
            {
                "timestamp": ist_timestamp_iso(),
                "event_type": EventType.NODE_STEP.value,
                "phase": "end",
                "node": node_name,
                "summary": summary or {},
                "error": error,
            },
        )


# Global logger instance
_logger: Optional[StructuredLogger] = None


def get_logger(log_dir: str = "logs", log_level: str = "INFO", enable_console: bool = True) -> StructuredLogger:
    """Get or create the global logger instance."""
    global _logger
    if _logger is None:
        _logger = StructuredLogger(log_dir=log_dir, log_level=log_level, enable_console=enable_console)
    return _logger
