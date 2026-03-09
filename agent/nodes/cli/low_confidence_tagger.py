"""
Low-confidence handler for CLI mode.

Informs the user when confidence is low. No Slack tagging.
"""

from typing import Dict, Any

from agent.state import ConversationState
from agent.logger import get_logger


def cli_low_confidence_tagger(state: ConversationState) -> ConversationState:
    """
    Handle low confidence in CLI mode.

    Instead of tagging team members (which requires Slack),
    just inform the user that confidence was low.

    Args:
        state: Current conversation state

    Returns:
        Updated state
    """
    logger = get_logger()

    user_id = state["user_id"]
    message_text = state["message_text"]
    confidence = state.get("research_confidence", 0.0)
    research_files = state.get("research_files", [])

    try:
        # Build a humble fallback response
        if not research_files:
            response_text = (
                "I wasn't able to find specific information in the codebase to address your question. "
                "This could mean:\n"
                "• The feature is implemented under a different name\n"
                "• The documentation hasn't been updated\n"
                "• Your question needs more context\n\n"
                "Could you share any additional details or error messages?"
            )
        else:
            # Found some files but confidence is low
            response_text = (
                f"I found {len(research_files)} potentially relevant file(s), "
                "but I'm not fully confident this answers your question.\n\n"
                "Here's what I found:\n"
            )
            for f in research_files[:5]:
                path = f.path if hasattr(f, "path") else f.get("path", "")
                reason = f.retrieval_reason if hasattr(f, "retrieval_reason") else f.get("retrieval_reason", "")
                response_text += f"• `{path}` — {reason}\n"

            response_text += "\nLet me know if you'd like me to search for something more specific."

        state["response_text"] = response_text

        logger.logger.info(
            f"[CLILowConfidenceTagger] low confidence response confidence={confidence:.2f}"
        )

    except Exception as e:
        logger.log_error(
            error_type="CLILowConfidenceTaggerError",
            error_message=str(e),
            user_id=user_id,
            channel_id="cli_channel",
        )
        state["error"] = str(e)
        state["response_text"] = "I encountered an error processing your question. Please try again."

    return state
