"""
Solution Provider Node — Formats and sends the final answer in-thread.

This node is invoked when research_confidence is high (>= 0.8). It strictly provides
a solution based on research_files (from codebase search).
"""

from __future__ import annotations
import asyncio
import json
from datetime import datetime
from typing import Any, List

from agent.state import ConversationState, ConversationRecord
from agent.slack_client import create_slack_client
from agent.persistence import get_database
from agent.logger import get_logger
from agent.llm import get_chat_completion
from agent.config import ABOUT_OLAKE


_SOLUTION_SYSTEM_TEMPLATE = """You are Alex, a senior support engineer on the OLake team.

About OLake:
{about_olake}

Identity and voice:
  - You speak as part of OLake: use "we" and "our", never "they" or "their" for OLake.
  - Talk like a human teammate in Slack, not like an AI assistant.
  - Never mention "the docs", "the reference files", "retrieved documents", "according to the information provided", or similar meta phrases.
  - Do not describe your process (no "I looked at the docs", "based on these files", etc.). Just answer directly as if you already know the product well.

Your job: Answer the user's question using ONLY the internal files/code snippets provided. You MUST base your answer on that context.

Rules:
  - If the files contain information that addresses the question, give a clear, concrete answer. Optional short follow-up question is fine.
  - If the files do not contain enough to answer safely, respond with 1-3 specific clarifying questions. Do not guess or make up information.
  - Lead with the substance (answer or questions), no preambles like "Sure" or "Great question".
  - Use "you/your" when talking to the user. For procedural guidance, use short numbered or bulleted lists.
  - Under ~300 words. Do not say "Based on the docs", "According to the info provided", "From the code", or any similar meta wording.
  - Direct answer first: For yes/no or single-fact questions, give the answer in one clear sentence first, then add brief context or one short follow-up if needed. Do not lead with long explanation or multiple clarifying questions when a direct answer is possible from the context.
  - Terminology and setup: Use the About OLake description when describing how OLake connects to sources and when answering questions about source-side settings or topology. When clarifying the user's setup, ask about which upstream OLake is connected to or how that upstream is configured (e.g. "Which instance is OLake reading from?" or "What sync mode are you using?"). Do not invent roles for OLake; follow the roles and connection model described in About OLake.

Return only the final Slack message text — no JSON, no markdown fences."""


def _solution_system_prompt(state: ConversationState) -> str:
    about = (state.get("about_olake_summary") or ABOUT_OLAKE).strip()
    return _SOLUTION_SYSTEM_TEMPLATE.format(about_olake=about)


def _build_history_block(state: ConversationState, max_messages: int = 6) -> str:
    """Build a short conversation history snippet from previous messages and thread context."""
    lines: List[str] = []
    thread_messages = state.get("thread_context", []) or []
    for msg in thread_messages[-max_messages:]:
        user = msg.get("user") or msg.get("username") or "user"
        text = (msg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"- {user}: {text}")
    if not lines:
        return "(none)"
    return "\n".join(lines[-max_messages:])


def _build_files_block(research_files: List[Any], max_files: int = 8, max_snippet: int = 220) -> str:
    """Summarise retrieved files for the LLM."""
    if not research_files:
        return "(none)"
    parts: List[str] = []
    for idx, f in enumerate(research_files[:max_files], 1):
        path = getattr(f, "path", "")
        reason = getattr(f, "retrieval_reason", "") or ""
        content = getattr(f, "content", "") or ""
        snippet = content.strip().replace("\n", " ")[:max_snippet]
        parts.append(f"{idx}. {path}\n   Why: {reason}\n   Snippet: {snippet}")
    return "\n".join(parts)


async def _generate_solution(state: ConversationState) -> str:
    """Generate the final answer from context. Always returns a solution (answer or clarifying questions)."""
    message_text = state["message_text"]
    problem_summary = state.get("problem_summary") or message_text
    research_files = state.get("research_files", [])

    history_block = _build_history_block(state)
    files_block = _build_files_block(research_files)

    user_prompt = f"""User question:
{message_text}

Problem summary (may be approximate):
{problem_summary}

Conversation so far (most recent messages last):
{history_block}

Internal files and code snippets to use for your answer:
{files_block}

Answer the question using the internal files above. If the files do not contain enough information, ask 1-3 short clarifying questions. Do not mention the files explicitly; just answer or ask.

Now write the final Slack message text."""

    response = await get_chat_completion(
        messages=[
            {"role": "system", "content": _solution_system_prompt(state)},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
    )
    text = (response or "").strip()
    return text


def solution_provider(state: ConversationState) -> ConversationState:
    """
    Provide the final answer in-thread. Always returns a solution based on research_files.
    No escalation; this node is only invoked when we have sufficient context (high research_confidence).
    """
    logger = get_logger()
    db = get_database()

    user_id = state["user_id"]
    channel_id = state["channel_id"]
    thread_ts = state.get("thread_ts") or state["message_ts"]
    message_text = state["message_text"]

    try:
        slack_client = create_slack_client()

        slack_client.add_reaction(
            channel=channel_id,
            timestamp=state["message_ts"],
            emoji="white_check_mark",
        )

        # When DeepResearcher hit an error it sets research_error + response_text; send that instead of calling LLM
        if state.get("research_error") and state.get("response_text"):
            final_message = (state["response_text"] or "").strip()
            state["response_text"] = final_message
            blocks = slack_client.format_response_blocks(
                response_text=final_message,
                confidence=state.get("research_confidence", 0.0),
                docs_cited=None,
                is_clarification=False,
                is_escalation=False,
            )
            state["response_blocks"] = blocks
            slack_client.send_message(channel=channel_id, text=final_message, thread_ts=thread_ts, blocks=blocks)
            logger.logger.info(f"[SolutionProvider] sent error fallback message len={len(final_message)}")
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
                    escalated=False,
                    escalation_reason=None,
                    docs_cited=None,
                    reasoning_summary=state.get("reasoning_trace", ""),
                    processing_time=processing_time,
                    created_at=datetime.now(),
                    resolved=True,
                    resolved_at=datetime.now(),
                    retrieval_queries=retrieval_queries,
                    retrieval_file_paths=retrieval_file_paths,
                ))
            except Exception:
                pass
            return state

        try:
            final_message = asyncio.run(_generate_solution(state))
        except Exception as gen_err:
            logger.logger.warning(f"[SolutionProvider] LLM call failed: {gen_err}. Sending minimal fallback.")
            final_message = ""

        final_message = (final_message or "").strip()
        state["response_text"] = final_message

        blocks = slack_client.format_response_blocks(
            response_text=final_message,
            confidence=state.get("research_confidence", 0.0),
            docs_cited=None,
            is_clarification=False,
            is_escalation=False,
        )
        state["response_blocks"] = blocks

        slack_client.send_message(
            channel=channel_id,
            text=final_message,
            thread_ts=thread_ts,
            blocks=blocks,
        )

        logger.logger.info(f"[SolutionProvider] sent answer len={len(final_message)}")

        try:
            processing_time = (datetime.now() - state["processing_start_time"]).total_seconds()
            retrieval_queries = json.dumps(state.get("retrieval_history", []))
            retrieval_file_paths = json.dumps([f.path for f in state.get("research_files", [])])
            needs_clarification = "?" in final_message
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
                needs_clarification=needs_clarification,
                escalated=False,
                escalation_reason=None,
                docs_cited=None,
                reasoning_summary=state.get("reasoning_trace", ""),
                processing_time=processing_time,
                created_at=datetime.now(),
                resolved=True,
                resolved_at=datetime.now(),
                retrieval_queries=retrieval_queries,
                retrieval_file_paths=retrieval_file_paths,
            ))
        except Exception:
            pass

    except Exception as e:
        logger.log_error(
            error_type="SolutionProviderError",
            error_message=str(e),
            user_id=user_id,
            channel_id=channel_id,
        )

    return state
