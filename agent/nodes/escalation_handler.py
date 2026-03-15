"""
Escalation Handler — Ask the LLM to choose one team member for the query, then @mention them.
"""

from __future__ import annotations
import json
from datetime import datetime

from agent.state import ConversationState, ConversationRecord
from agent.slack_client import create_slack_client
from agent.persistence import get_database
from agent.logger import get_logger
from agent.team import get_all_members_flat, resolve_mention
from agent.llm import get_chat_completion_sync


def escalation_handler(state: ConversationState) -> ConversationState:
    """
    Send the team list and user query to the LLM; it chooses one person. Tag them in-thread.
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

        members = get_all_members_flat()
        if not members:
            logger.logger.info("[EscalationHandler] no team members; skipping reply")
            state["response_text"] = None
            return state

        team_list = "\n".join(
            f"- {m['slack_name']} ({m.get('role', '')}, {m.get('dept', '')})"
            for m in members
        )
        prompt = (
            "You are escalating a user question to exactly one team member. "
            "Choose the best person to help. Reply with only that person's slack_name, nothing else.\n\n"
            "Team:\n" + team_list
        )
        query = f"User question: {message_text or '(no message)'}"

        try:
            reply = get_chat_completion_sync(
                [
                    {"role": "user", "content": f"{prompt}\n\n{query}"},
                ],
                temperature=0.2,
                max_tokens=100,
            )
        except Exception as e:
            logger.logger.warning(f"[EscalationHandler] LLM failed: {e}")
            state["response_text"] = None
            return state

        name = (reply or "").strip().split("\n")[0].strip()
        if not name:
            state["response_text"] = None
            return state

        # Match to a team member (case-insensitive) for proper mention
        for m in members:
            if m["slack_name"].lower() == name.lower():
                name = m["slack_name"]
                break
        final_message = resolve_mention(name)

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
        logger.logger.info(f"[EscalationHandler] tagged: {name}")

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
                response_text=state.get("response_text"),
                confidence=state.get("research_confidence", 0.0),
                needs_clarification=False,
                escalated=True,
                escalation_reason=state.get("response_text") or "Skipped (no message sent)",
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
