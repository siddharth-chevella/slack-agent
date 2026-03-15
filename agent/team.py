"""
Minimal team utilities: load olake-team.json, bot user ID, org-member detection, and mention resolution.

Used by main (startup), context_builder (org_member_replied), and escalation (resolve_mention).
Escalation routing itself is done in escalation_handler via LLM (prompt + query).
"""

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from agent.config import Config

log = logging.getLogger(__name__)

_team_data: Dict = {}
_all_members: List[Dict] = []
_slack_name_to_id: Dict[str, str] = {}
_bot_user_id: Optional[str] = None


def load_team() -> Dict:
    """Load team data from olake-team.json."""
    global _team_data, _all_members
    path = Config.TEAM_FILE
    try:
        raw = json.loads(path.read_text())
        _team_data = raw
        _all_members = []
        for dept, members in raw.items():
            for slack_name, info in members.items():
                _all_members.append({
                    "slack_name": slack_name,
                    "role": info.get("role", ""),
                    "desc": info.get("desc", ""),
                    "dept": dept,
                })
        log.info(f"Loaded {len(_all_members)} team members from {path}")
        return raw
    except Exception as e:
        log.error(f"Failed to load {path}: {e}")
        return {}


def build_name_to_id_cache(slack_users: List[Dict]) -> None:
    """Build slack_name → user_id from Slack users.list."""
    global _slack_name_to_id
    name_to_id: Dict[str, str] = {}
    for u in slack_users:
        uid = u.get("id", "")
        profile = u.get("profile", {})
        for field in ("display_name", "real_name", "name"):
            val = (profile.get(field) or u.get(field) or "").strip()
            if val:
                name_to_id[val.lower()] = uid
    cache = {}
    for m in _all_members:
        sn = m["slack_name"]
        uid = name_to_id.get(sn.lower())
        if uid:
            cache[sn] = uid
        else:
            log.warning(f"Could not resolve Slack user ID for slack_name='{sn}'")
    _slack_name_to_id = cache
    log.info(f"Resolved {len(cache)}/{len(_all_members)} team members to Slack IDs")


def set_bot_user_id(user_id: str) -> None:
    global _bot_user_id
    _bot_user_id = user_id
    log.info(f"Bot user ID set to {user_id}")


def get_bot_user_id() -> Optional[str]:
    return _bot_user_id


def is_org_member_by_name(display_name: str) -> bool:
    if not _all_members:
        load_team()
    dn = display_name.strip().lower()
    return any(m["slack_name"].lower() == dn for m in _all_members)


def is_org_member_by_id(user_id: str) -> bool:
    return bool(user_id) and user_id in _slack_name_to_id.values()


def get_all_members_flat() -> List[Dict]:
    """Flattened list of {slack_name, role, desc, dept} for prompt building."""
    if not _all_members:
        load_team()
    return list(_all_members)


def resolve_mention(slack_name: str) -> str:
    """Return '<@USERID>' if resolved, else '@slack_name'."""
    uid = _slack_name_to_id.get(slack_name)
    return f"<@{uid}>" if uid else f"@{slack_name}"
