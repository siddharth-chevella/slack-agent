"""
Solution provider for CLI mode.

Formats the final answer and persists to DB. No Slack API (no send_message).
"""

from __future__ import annotations
import asyncio
import json
from datetime import datetime
from typing import Dict, Any, List

from agent.state import ConversationState, ConversationRecord
from agent.persistence import get_database
from agent.logger import get_logger
from agent.llm import get_chat_completion
from agent.config import ABOUT_OLAKE
from agent.utils.parser import JSON_FENCE_RE as _PARSE_RE_FENCE

_SENTENCE_ENDINGS = (".", "!", "?", "…", ":", "```")


def _looks_truncated(text: str) -> bool:
    """Return True if text appears to end mid-sentence."""
    stripped = text.rstrip()
    return bool(stripped) and not stripped.endswith(_SENTENCE_ENDINGS)


_ALEX_SYSTEM_TEMPLATE = """You are Alex, a senior support engineer on the OLake/Datazip team. You answer as part of OLake — use "we" and "our". Never refer to OLake as "they" or "their."

About OLake:
{about_olake}

Important: OLake is one product (data ingestion to Iceberg). Do not say "our tools" (plural). Say "OLake" or "we" (e.g. "We focus on…", "OLake doesn't support X").

Your job: turn the draft and reference files into a final reply.

Voice and grounding:
  - Speak as a teammate. Base your answer ONLY on the reference files. If the files show a feature exists in some form (e.g. per-stream), say what is supported and the limitation — do not say "we don't support X" if the files show X in a limited way. If the files don't describe a feature at all (e.g. snapshot deletion), say: we don't provide that, or it's done at the Iceberg/query-engine level. Do not invent or direct to "our docs" as the main answer.
  - Only mention files that are relevant to what you said.

Tone: Professional and human. Lead with the answer. Use "you/your". No generic openers or "Let me know if you have further questions." One emoji max. Under 300 words.

Return the final message text only — no JSON, no markdown wrapper."""


def _alex_system(state: ConversationState) -> str:
    about = (state.get("about_olake_summary") or ABOUT_OLAKE).strip()
    return _ALEX_SYSTEM_TEMPLATE.format(about_olake=about)


def _build_doc_citations(docs: list) -> str:
    """Build a short doc-reference block (max 3 docs)."""
    if not docs:
        return ""

    lines = []
    seen_paths = set()

    # Group docs by type
    doc_files = []
    code_files = []

    for doc in docs:
        path = doc.path if hasattr(doc, "path") else doc.get("path", "")
        if path in seen_paths:
            continue
        seen_paths.add(path)

        # Categorize
        if path.endswith(('.md', '.rst', '.txt')) or 'doc' in path.lower() or 'example' in path.lower():
            doc_files.append(path)
        else:
            code_files.append(path)

    # Build citations with GitHub links for code files
    for path in doc_files[:2]:
        if path.startswith('http'):
            lines.append(f"• {path}")
        else:
            # Convert to GitHub link
            gh_path = f"https://github.com/datazip-inc/olake/blob/main/{path}"
            lines.append(f"• `{path}` — {gh_path}")

    for path in code_files[:2]:
        gh_path = f"https://github.com/datazip-inc/olake/blob/main/{path}"
        lines.append(f"• `{path}` — {gh_path}")

    if not lines:
        return ""

    return "\n\n📚 *Relevant files:*\n" + "\n".join(lines)


async def _polish_answer(
    draft: str,
    user_query: str,
    files: list,
    confidence: float,
    is_conceptual: bool = False,
    state: ConversationState | None = None,
) -> str:
    """Optional LLM polish pass."""
    file_context = ""
    if files:
        snippets = []
        for f in files[:4]:
            content = f.content if hasattr(f, "content") else f.get("content", "")
            path = f.path if hasattr(f, "path") else f.get("path", "")
            snippets.append(f"[REFERENCE FILE — do not copy verbatim]\nPath: {path}\n{content[:300]}")
        file_context = "\n\n---\n".join(snippets)

    conceptual_instruction = ""
    if is_conceptual:
        conceptual_instruction = "\n\nThis is a conceptual question — answer from your knowledge about OLake without needing file references."

    prompt = f"""User question: "{user_query}"
{conceptual_instruction}

Draft answer (refine this — if empty, write from scratch using ONLY the reference files below):
{draft or "(none — use reference files below)"}

Reference files (base your answer only on these — do NOT copy verbatim):
{file_context or "(none available)"}

If the reference files do NOT describe what the user asked for, say so. If they DO show a related feature (e.g. partition_regex per stream), describe what is supported and the limitation. Refer to OLake as "we" or "OLake", never "our tools."

Confidence: {confidence:.0%}

Write the final reply. Start with the answer — no preamble."""

    system = _alex_system(state) if state else _ALEX_SYSTEM_TEMPLATE.format(about_olake=ABOUT_OLAKE.strip())
    response = await get_chat_completion(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        temperature=0.4,
    )
    return (response or "").strip()


def cli_solution_provider(state: ConversationState) -> ConversationState:
    """
    Format final answer for CLI mode.
    """
    logger = get_logger()

    user_id = state["user_id"]
    user_query = state["user_query"]
    response_text = state.get("response_text", "") or ""
    confidence = state.get("research_confidence", 0.0)
    research_files = state.get("research_files", [])
    is_conceptual = state.get("is_conceptual", False)

    try:
        # Decide whether to polish
        needs_polish = (
            not response_text
            or len(response_text) < 80
            or confidence < 0.45
            or _looks_truncated(response_text)
        )

        if needs_polish:
            try:
                response_text = asyncio.run(_polish_answer(
                    draft=response_text,
                    user_query=user_query,
                    files=research_files,
                    confidence=confidence,
                    is_conceptual=is_conceptual,
                    state=state,
                ))
            except Exception as polish_err:
                logger.logger.warning(f"[CLISolutionProvider] Polish LLM call failed: {polish_err}. Using fallback.")
                if research_files:
                    file_list = ", ".join(f"`{getattr(f, 'path', str(f))}`" for f in research_files[:5])
                    response_text = (
                        f"Relevant files found: {file_list}. "
                        "Could not generate a full summary right now (temporary issue). Try again in a moment or inspect the files above."
                    )
                else:
                    response_text = "Could not generate a full answer right now (temporary issue). Please try again in a moment."

        # Low-confidence soft note
        if confidence < 0.6:
            response_text += "\n\nIf this doesn't fully address your issue, let me know — I may need more context."

        # Do not include relevant files in the response
        final_message = response_text.strip()
        state["response_text"] = final_message

        logger.logger.info(
            f"[CLISolutionProvider] generated answer confidence={confidence:.2f} len={len(final_message)}"
        )

        # Persist conversation and retrieval context (same as Slack path)
        try:
            processing_time = (datetime.now() - state["processing_start_time"]).total_seconds()
            retrieval_queries = json.dumps(state.get("retrieval_history", []))
            retrieval_file_paths = json.dumps([f.path for f in research_files])
            docs_cited_list = [
                {"title": getattr(f, "path", str(f)), "url": f"https://github.com/datazip-inc/olake/blob/main/{getattr(f, 'path', '')}", "source": getattr(f, "source", "ripgrep")}
                for f in research_files[:3]
            ]
            db = get_database()
            db.save_conversation(ConversationRecord(
                id=None,
                message_ts=state["message_ts"],
                thread_ts=state.get("thread_ts"),
                channel_id=state.get("channel_id", "cli_channel"),
                user_id=user_id,
                user_query=user_query,
                intent_type=str(state.get("intent_type", "")),
                response_text=final_message,
                confidence=confidence,
                needs_clarification=False,
                escalated=False,
                escalation_reason=None,
                docs_cited=json.dumps(docs_cited_list),
                reasoning_summary=state.get("reasoning_trace", ""),
                processing_time=processing_time,
                created_at=datetime.now(),
                resolved=True,
                resolved_at=datetime.now(),
                retrieval_queries=retrieval_queries,
                retrieval_file_paths=retrieval_file_paths,
            ))
        except Exception as e:
            logger.logger.warning(f"[CLISolutionProvider] Failed to persist conversation: {e}")

    except Exception as e:
        logger.log_error(
            error_type="CLISolutionProviderError",
            error_message=str(e),
            user_id=user_id,
            channel_id="cli_channel",
        )

    return state
