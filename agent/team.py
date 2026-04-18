"""
Team utilities: load config/team.json, bot user ID, and org-member detection.

Used by main (startup) to skip messages sent by org members.
"""

import json
import logging
from typing import Dict, List, Optional

from agent.config import Config

log = logging.getLogger(__name__)

_team_data: Dict = {}
_all_members: List[Dict] = []
_slack_name_to_id: Dict[str, str] = {}
_bot_user_id: Optional[str] = None


def load_team() -> Dict:
    """Load team data from config/team.json."""
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
        log.info("Loaded %d team members from %s", len(_all_members), path)
        return raw
    except Exception as e:
        log.error("Failed to load %s: %s", path, e)
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
            log.warning("Could not resolve Slack user ID for slack_name=%r", sn)
    _slack_name_to_id = cache
    log.info("Resolved %d/%d team members to Slack IDs", len(cache), len(_all_members))


def set_bot_user_id(user_id: str) -> None:
    global _bot_user_id
    _bot_user_id = user_id
    log.info("Bot user ID set to %s", user_id)


def get_bot_user_id() -> Optional[str]:
    return _bot_user_id


def is_org_member_by_id(user_id: str) -> bool:
    return bool(user_id) and user_id in _slack_name_to_id.values()


def get_all_members_flat() -> List[Dict]:
    """Flattened list of {slack_name, role, desc, dept} for prompt building."""
    if not _all_members:
        load_team()
    return list(_all_members)
