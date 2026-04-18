"""
In-process deduplication for Slack events.

Slack retries events aggressively (via X-Slack-Retry-Num) whenever a response is
slow or drops. Without dedup, the agent can reply twice to the same user message.

Two independent guards are exposed:
  - `is_retry(headers)` — short-circuits requests that Slack has flagged as retries.
  - `seen(event_id)`    — idempotency check against a bounded FIFO set keyed on
                          `client_msg_id` (falls back to `event_ts`).

Both helpers are thread-safe. State is per-process; restarting the agent clears it.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Mapping, Optional


_MAX_ENTRIES = 4096

_lock = Lock()
# OrderedDict used as a bounded FIFO set: keys are event IDs, values ignored.
_seen: "OrderedDict[str, None]" = OrderedDict()


def is_retry(headers: Mapping[str, str]) -> bool:
    """Return True when Slack has flagged the inbound request as a retry."""
    retry_num = (headers.get("X-Slack-Retry-Num") or "").strip()
    return bool(retry_num)


def event_id_from(event: Mapping[str, object]) -> Optional[str]:
    """Best-effort stable identifier for a Slack message event."""
    cmid = event.get("client_msg_id")
    if isinstance(cmid, str) and cmid:
        return cmid
    ets = event.get("event_ts") or event.get("ts")
    if isinstance(ets, str) and ets:
        return f"ts:{ets}"
    return None


def seen(event_id: Optional[str]) -> bool:
    """
    Record `event_id` and return True if it had already been recorded.
    A None/empty id is never deduped (returns False) so callers don't silently drop
    events they can't identify.
    """
    if not event_id:
        return False
    with _lock:
        if event_id in _seen:
            _seen.move_to_end(event_id)
            return True
        _seen[event_id] = None
        if len(_seen) > _MAX_ENTRIES:
            _seen.popitem(last=False)
        return False


def reset() -> None:
    """Clear the dedup cache. Intended for tests."""
    with _lock:
        _seen.clear()
