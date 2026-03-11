"""
Escalation Handler Node — Tags relevant team members based on the question.

Uses team_resolver (keyword → department → members) to pick who to escalate to,
then sends an in-thread message that @mentions them. No LLM; routing is deterministic.
"""

from __future__ import annotations
import json
from datetime import datetime
from typing import Dict, Any

from agent.state import ConversationState, ConversationRecord
from agent.slack_client import create_slack_client
from agent.persistence import get_database
from agent.logger import get_logger
from agent.team_resolver import get_escalation_targets, format_escalation_message


def escalation_handler(state: ConversationState) -> ConversationState:
    """
    Tag relevant team members in-thread based on the user question.
    Uses get_escalation_targets(message_text) and format_escalation_message.
    """
    logger = get_logger()
    db = get_database()

    channel_id = state["channel_id"]
    user_id = state["user_id"]
    thread_ts = state.get("thread_ts") or state["message_ts"]
    message_text = state["message_text"]

    try:
        slack_client = create_slack_client()
        slack_client.add_reaction(
            channel=channel_id,
            timestamp=state["message_ts"],
            emoji="white_check_mark",
        )

        targets = get_escalation_targets(issue_text=message_text)
        final_message = format_escalation_message(targets, issue_summary=message_text)
        state["response_text"] = final_message
        state["response_blocks"] = slack_client.format_response_blocks(
            response_text=final_message,
            confidence=0.0,
            is_clarification=False,
            is_escalation=True,
        )

        slack_client.send_message(
            channel=channel_id,
            text=final_message,
            thread_ts=thread_ts,
            blocks=state["response_blocks"],
        )

        logger.logger.info(
            f"[EscalationHandler] tagged {len(targets)} member(s): "
            f"{[t['slack_name'] for t in targets]}"
        )

        try:
            processing_time = (datetime.now() - state["processing_start_time"]).total_seconds()
            retrieval_queries = json.dumps(state.get("retrieval_history", []))
            retrieval_file_paths = json.dumps([f.path for f in state.get("research_files", [])])
            db.save_conversation(ConversationRecord(
                id=None,
                message_ts=state["message_ts"],
                thread_ts=thread_ts,
                channel_id=channel_id,
                user_id=user_id,
                message_text=message_text,
                intent_type=str(state.get("intent_type", "")),
                urgency=str(state.get("urgency", "")),
                response_text=final_message,
                confidence=state.get("research_confidence", 0.0),
                needs_clarification=False,
                escalated=True,
                escalation_reason=final_message,
                docs_cited=None,
                reasoning_summary=state.get("reasoning_trace", ""),
                processing_time=processing_time,
                created_at=datetime.now(),
                resolved=False,
                resolved_at=None,
                retrieval_queries=retrieval_queries,
                retrieval_file_paths=retrieval_file_paths,
            ))
        except Exception:
            pass

    except Exception as e:
        logger.log_error(
            error_type="EscalationHandlerError",
            error_message=str(e),
            user_id=user_id,
            channel_id=channel_id,
        )

    return state
