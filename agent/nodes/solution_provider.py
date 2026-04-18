"""
Solution Provider Node — Generates and sends the final answer in-thread.

Always invoked as the terminal node. Handles three cases:
  1. deep_researcher hit an error → forward the error message as-is
  2. No relevant files found (and not a conceptual question) → explicitly say so
  3. Normal path → call LLM with retrieved context and post the answer
"""

from __future__ import annotations
import asyncio
import logging
from typing import Any, List

from agent.state import ConversationState
from agent.slack_client import create_slack_client
from agent.persistence import get_database
from agent.logger import get_logger
from agent.llm import get_chat_completion
from agent.config import ABOUT_COMPANY, AGENT_NAME, COMPANY_NAME, COMPANY_VOICE

log = logging.getLogger(__name__)

_NOT_ENOUGH_INFO = (
    "Something went wrong. Please try again later."
)

def _solution_system_prompt() -> str:
    return f"""CRITICAL — Voice: {COMPANY_VOICE}

You are {AGENT_NAME}, a senior support engineer on the {COMPANY_NAME} team.

About {COMPANY_NAME}:
{ABOUT_COMPANY}

Identity and voice:
  - {COMPANY_VOICE}
  - Talk like a human teammate in Slack, not like an AI assistant.
  - Never mention "the docs", "the reference files", "retrieved documents", "the codebase", or similar meta phrases.
  - Do not describe your process. Just answer directly as if you already know the product well.

Your job: Answer the user's question using the internal files/code snippets provided below.

Scope of what you can recommend (IMPORTANT):
  - Users run OLake through one of a few surfaces: the OLake UI, the CLI / Docker, or a Helm / Kubernetes deployment. The knobs available to them differ by surface, and most engine internals are NOT user-configurable on any surface.
  - Only recommend settings, features, or steps that are DOCUMENTED for users (i.e. appear in olake-docs content, in user-facing JSON/YAML config examples, or are clearly described as a user-facing option in the retrieved context).
  - Do NOT surface Go struct fields, private identifiers, internal constants, or engine-level concepts (e.g. batch size, chunking strategy, writer pool, global/stream/writer concurrency tiers, thread counts that are not an exposed flag) as if they were user knobs. If you only found something in the `olake` core repo and there is no corresponding user-facing mention in `olake-docs`, treat it as internal and do not instruct the user to change it.
  - If a recommendation depends on which surface the user is on (UI vs CLI/Docker vs Helm) and their surface is not clear from the conversation, briefly ask which one they're using before giving specific steps. Keep the clarifying question to one short sentence.
  - Describe outcomes in plain language ("increase the parallelism for large-table full refreshes", "configure the source connection to use a replica"). Do NOT prescribe specific UI clicks ("click the X button") or file-level edits ("edit state.json line 42") — point to the relevant docs page instead.

Rules:
  - ONLY state things that are explicitly supported by the provided context. Do not invent, infer, or assume anything not directly present in the context — if it isn't there, don't say it.
  - If the context doesn't contain enough to answer, ask the user for more information/clarification rather than guessing.
  - Lead with the substance, no preambles like "Sure" or "Great question".
  - Use "you/your" when talking to the user. For procedural steps, use short numbered or bulleted lists.
  - Keep the response concise and to the point.
  - DO NOT reference any code snippets or raw code. Refer only to configuration that a user can actually change — described in plain language.
  - For how-to / configuration / "is X supported?" questions, prefer linking to the relevant `https://olake.io/docs/...` page over re-explaining everything inline. If a retrieved file has a `doc_url:` next to it, that URL is trustworthy — use it verbatim.
  - If your searches doesn't include any information, do not say 'the context I have doesn't include' or similar. Rather use phrasing like 'I did not find any information regarding...' or similar.
  - If user is appreciating something then respond with gratitude.

NOTE:
    - OLake docs are available at https://olake.io/docs. When retrieved context contains `doc_url:` hints for `olake-docs/docs/...` files, those URLs are safe to cite verbatim. You MAY also append a section anchor (e.g. `#configuration`) only if that anchor is clearly present in the retrieved content.
    - DO NOT guess or fabricate URLs. If you don't have a `doc_url` hint or a verbatim URL in the context, don't include a link.

Return only the final Slack message text — no JSON, no markdown fences."""

def _build_history_block(state: ConversationState, max_messages: int = 6) -> str:
    lines: List[str] = []
    for msg in (state.get("thread_context") or [])[-max_messages:]:
        text = (msg.get("content") or "").strip()
        if not text:
            continue
        label = "agent" if msg.get("role") == "agent" else "user"
        lines.append(f"- {label}: {text}")
    return "\n".join(lines) if lines else "(none)"


def _build_files_block(research_files: List[Any], max_files: int = 8, max_snippet: int = 300) -> str:
    if not research_files:
        return "(none)"
    parts: List[str] = []
    for idx, f in enumerate(research_files[:max_files], 1):
        path = getattr(f, "path", "")
        reason = getattr(f, "retrieval_reason", "") or ""
        doc_url = getattr(f, "doc_url", None)
        snippet = (getattr(f, "content", "") or "").strip().replace("\n", " ")[:max_snippet]
        url_line = f"\n   doc_url: {doc_url}" if doc_url else ""
        parts.append(f"{idx}. {path}{url_line}\n   Why: {reason}\n   Snippet: {snippet}")
    return "\n".join(parts)


def _has_docs_coverage(research_files: List[Any]) -> bool:
    """True if at least one retrieved file is a user-facing docs page."""
    for f in research_files or []:
        if getattr(f, "doc_url", None):
            return True
        path = getattr(f, "path", "") or ""
        if path.startswith("olake-docs/docs/"):
            return True
    return False


async def _generate_solution(state: ConversationState) -> str:
    user_query = state["user_query"]
    research_files = state.get("research_files") or []
    thread_summary = state.get("thread_summary") or ""
    research_summary = (state.get("research_summary") or "").strip()

    history_block = _build_history_block(state)
    files_block = _build_files_block(research_files)

    summary_section = f"\nThread summary (earlier context):\n{thread_summary}\n" if thread_summary else ""
    research_summary_section = (
        f"\nResearch Summary (what was searched):\n{research_summary}\n"
        if research_summary else ""
    )

    surface_hint = ""
    if research_files and not _has_docs_coverage(research_files):
        surface_hint = (
            "\nRETRIEVAL HINT: The retrieved context is mostly engine-internal code (no "
            "olake-docs pages were matched). Treat these as internals, NOT user-facing knobs. "
            "If the user is asking how to tune / configure / change behaviour and their "
            "surface (UI vs CLI/Docker vs Helm) is not clear from the thread, ask which one "
            "they're using in one short sentence before prescribing any specific change.\n"
        )

    user_prompt = f"""User question:
{user_query}
{summary_section}{research_summary_section}{surface_hint}
Conversation so far (most recent messages last):
{history_block}

Internal files and code snippets:
{files_block}

Answer the question using the context above. If the Research Summary lists the user's error string under \
"Null results", apply the NULL-RESULT GUIDANCE from your system prompt.

Now write the final Slack message."""

    response = await get_chat_completion(
        messages=[
            {"role": "system", "content": _solution_system_prompt()},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.4,
    )
    return (response or "").strip()


def solution_provider(state: ConversationState) -> ConversationState:
    """Terminal node — always produces a response and sends it to the Slack thread."""
    logger = get_logger()
    db = get_database()

    user_id = state["user_id"]
    channel_id = state["channel_id"]
    thread_ts = state.get("thread_ts") or state["message_ts"]
    user_query = state["user_query"]

    research_files = state.get("research_files") or []
    is_conceptual = state.get("is_conceptual", False)
    log.debug("[SolutionProvider] start files=%d conceptual=%s", len(research_files), is_conceptual)

    try:
        slack_client = create_slack_client()

        # Case 1: deep_researcher encountered an unrecoverable error
        if state.get("research_error") and state.get("response_text"):
            final_message = (state["response_text"] or "").strip()
            log.debug("[SolutionProvider] research error fallback len=%d", len(final_message))
            slack_client.send_message(channel=channel_id, text=final_message, thread_ts=thread_ts)
            logger.log_response_sent(
                channel_id, final_message, thread_ts=thread_ts, source="solution_error_fallback"
            )
            _persist(db, logger, thread_ts, user_id, user_query, state["message_ts"], final_message)

            return state

        # Case 2: no files found and it wasn't a conceptual question → explicit admission
        if not research_files and not is_conceptual:
            log.debug("[SolutionProvider] no files, not conceptual — sending not-enough-info")
            slack_client.send_message(channel=channel_id, text=_NOT_ENOUGH_INFO, thread_ts=thread_ts)
            logger.log_response_sent(
                channel_id, _NOT_ENOUGH_INFO, thread_ts=thread_ts, source="solution_not_enough_info"
            )
            state["response_text"] = _NOT_ENOUGH_INFO
            _persist(db, logger, thread_ts, user_id, user_query, state["message_ts"], _NOT_ENOUGH_INFO)
            return state

        # Case 3: normal path — generate answer with LLM
        log.debug("[SolutionProvider] generating answer files=%d", len(research_files))
        try:
            final_message = asyncio.run(_generate_solution(state))
        except Exception as gen_err:
            logger.logger.debug("[SolutionProvider] LLM call failed: %s", gen_err)
            final_message = _NOT_ENOUGH_INFO

        final_message = (final_message or _NOT_ENOUGH_INFO).strip()
        state["response_text"] = final_message

        log.debug("[SolutionProvider] answer len=%d", len(final_message))
        slack_client.send_message(channel=channel_id, text=final_message, thread_ts=thread_ts)
        logger.log_response_sent(channel_id, final_message, thread_ts=thread_ts, source="solution")

        _persist(db, logger, thread_ts, user_id, user_query, state["message_ts"], final_message)

    except Exception as e:
        logger.log_error(
            error_type="SolutionProviderError",
            error_message=str(e),
            user_id=user_id,
            channel_id=channel_id,
        )

    return state


def _persist(db, logger, thread_ts: str, user_id: str, user_query: str, message_ts: str, agent_reply: str) -> None:
    try:
        db.save_message(thread_ts, user_id, "user", user_query, message_ts)
        db.save_message(thread_ts, None, "agent", agent_reply, message_ts + "_agent")
        logger.logger.debug("[SolutionProvider] persisted user+agent messages")
    except Exception as persist_err:
        logger.logger.warning("[SolutionProvider] failed to persist messages: %s", persist_err)
