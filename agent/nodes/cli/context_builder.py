"""
Context builder for CLI mode.

Lightweight context: no database, no Slack API, no org-member detection.
"""

from typing import Dict, Any, List

from agent.state import ConversationState
from agent.logger import get_logger


def build_cli_context(state: ConversationState) -> ConversationState:
    """
    Build minimal context for CLI mode.

    - No database access
    - No Slack API calls
    - No org member detection
    - No user profile loading

    Args:
        state: Current conversation state

    Returns:
        Updated state with minimal context
    """
    logger = get_logger()

    user_id = state["user_id"]
    channel_id = state["channel_id"]

    # Set minimal context
    state["user_profile"] = None
    state["previous_messages"] = []
    state["thread_context"] = []
    state["org_member_replied"] = False  # Always False in CLI mode

    # Log for debugging
    logger.logger.debug(f"[CLI Context] Built minimal context for user {user_id}")

    return state
