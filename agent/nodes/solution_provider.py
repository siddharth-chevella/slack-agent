"""
Solution Provider Node — Formats and sends the final answer in-thread.

Uses research_files (from ripgrep/ast-grep codebase search) as context. No vectordb/RAG.
  - Polish pass when response_text is missing or low-quality
  - Cites up to 3 code/file sources (path + source: ripgrep | ast-grep)
  - Confidence from deep_researcher (research_confidence → final_confidence)
"""

from __future__ import annotations
import asyncio
import json
import re
from datetime import datetime
from typing import Dict, Any, List, Optional

from agent.state import ConversationState, ConversationRecord
from agent.slack_client import create_slack_client
from agent.persistence import get_database
from agent.logger import get_logger
from agent.llm import get_chat_completion
from agent.config import Config, OLAKE_CONTEXT

_PARSE_RE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$")

# Sentence-ending punctuation that signals a complete thought
_SENTENCE_ENDINGS = (".", "!", "?", "…", ":", "```")


def _looks_truncated(text: str) -> bool:
    """Return True if text appears to end mid-sentence (token-limit cut-off)."""
    stripped = text.rstrip()
    return bool(stripped) and not stripped.endswith(_SENTENCE_ENDINGS)

# Alex persona — you ARE part of OLake; answer as "we/our", never "they/their"
_ALEX_SYSTEM = f"""You are Alex, a senior support engineer on the OLake/Datazip team. You answer as part of OLake — use "we" and "our" (we don't provide X, we recommend). Never refer to OLake as "they" or "their" or "OLake's docs" as an outside party.

About OLake:
{OLAKE_CONTEXT.strip()}

Important: OLake is one product — a data ingestion tool to Apache Iceberg. Do not say "our tools" (plural). Say "OLake" or "we" (e.g. "OLake focuses on…" or "We focus on…", "we don't support X").

Your job: turn the draft and reference files into a final Slack reply.

Voice and grounding:
  - Speak as a human teammate: "We don't have a built-in snapshot cleanup" not "OLake doesn't provide...". "You can do this with Iceberg's expire_snapshots" not "consult their docs."
  - Base your answer ONLY on the reference files below. If the reference files show that a feature exists in some form (e.g. per-stream, per-table), say what is supported and what the limitation is — do not say "we don't support X" if the files show X exists in a limited way (e.g. "Partition regex is supported per stream in the catalog; there's no apply-to-all option" not "We don't support partition regex").
  - If the files don't describe a feature at all (e.g. snapshot deletion), say clearly: we don't provide that, or it's done at the Iceberg/query-engine level. Do not invent tools or direct users to "our docs" for steps unless the reference files actually contain that.
  - Only mention or imply file citations that are relevant to what you said. Do not list irrelevant links.

Tone:
  - Professional and human. Like messaging a colleague on Slack.
  - Lead with the answer. Use "you/your". No passive voice for instructions.
  - For step-by-step fixes: numbered list. For conceptual answers: 2-4 short paragraphs max.
  - No "Great question!" or "Certainly!" openers. No generic "Let me know if you have further questions" — use a specific follow-up if needed.
  - One emoji max (✅ ❌) only if it adds clarity. Under 300 words.

Do NOT quote reference text verbatim. Do NOT start mid-thought.

Return the final Slack message text only — no JSON, no markdown wrapper."""


def _research_files_to_docs(research_files: list) -> list:
    """Convert ResearchFile list to doc-like dicts for polish/citations (title, content, url, source_type)."""
    docs = []
    for f in research_files:
        docs.append({
            "title": f.path,
            "content": f.content or "",
            "url": "",  # Code files have no URL; citations show path only
            "source_type": f.source,
        })
    return docs


def _build_doc_citations(docs: list) -> str:
    """Build a short reference block (max 3). Supports doc-like dicts with optional url."""
    if not docs:
        return ""
    lines = []
    seen = set()
    for doc in docs[:3]:
        title = doc.get("title", doc.title if hasattr(doc, "title") else "file")
        url = doc.get("url") if isinstance(doc, dict) else (getattr(doc, "url", None) or "")
        key = url or title
        if key in seen:
            continue
        seen.add(key)
        if url:
            lines.append(f"• <{url}|{title}>")
        else:
            lines.append(f"• `{title}`")
    if not lines:
        return ""
    return "\n*Relevant files:*\n" + "\n".join(lines)


async def _polish_answer(
    draft: str,
    message_text: str,
    docs: list,
    confidence: float,
    problem_summary: str,
) -> str:
    """
    Optional LLM polish pass — only called when draft is missing or low-quality.
    """
    doc_context = ""
    if docs:
        snippets = []
        for d in docs[:4]:
            content = d.get("content", "") if isinstance(d, dict) else (getattr(d, "content", "") or d.get("text", ""))
            title = d.get("title", "") if isinstance(d, dict) else getattr(d, "title", "")
            # Label each ref (code/file) and limit size
            snippets.append(f"[REFERENCE — do not copy verbatim]\nFile: {title}\n{content[:400]}")
        doc_context = "\n\n---\n".join(snippets)

    prompt = f"""User question: "{message_text}"
Problem summary: {problem_summary}

Draft answer (refine this — if empty, write from scratch using ONLY the reference files below):
{draft or "(none — use reference files below to write a concise answer in your own words)"}

Reference files/code (base your answer only on these — do NOT copy verbatim):
{doc_context or "(none available)"}

If the reference files do NOT describe what the user asked for (e.g. snapshot cleanup), say so clearly. If the files DO show a related feature (e.g. partition_regex per stream in the catalog), describe what is supported and the limitation (e.g. "Partition regex is set per stream in the catalog; there's no single apply-to-all" — not "We don't support partition regex"). Refer to OLake as "we" or "OLake", never "our tools" (OLake is one product).

Confidence: {confidence:.0%}

Write the final Slack reply. Start with the answer — no preamble."""

    response = await get_chat_completion(
        messages=[
            {"role": "system", "content": _ALEX_SYSTEM},
            {"role": "user",   "content": prompt},
        ],
        temperature=0.4,
        # max_tokens=600,
    )
    return (response or "").strip()


def solution_provider(state: ConversationState) -> ConversationState:
    """
    Format and send the final answer.
    Uses asyncio.run to call the async polish pass when needed.
    """
    logger = get_logger()
    db = get_database()

    user_id       = state["user_id"]
    channel_id    = state["channel_id"]
    thread_ts     = state.get("thread_ts") or state["message_ts"]
    message_text  = state["message_text"]
    response_text = state.get("response_text", "") or ""
    confidence    = state.get("final_confidence", 0.0)
    research_files = state.get("research_files", [])
    docs_for_polish = _research_files_to_docs(research_files)  # Code search results (no vectordb)
    doc_sufficient = state.get("doc_sufficient", False)
    problem_summary = state.get("problem_summary") or message_text

    try:
        slack_client = create_slack_client()

        slack_client.add_reaction(
            channel=channel_id,
            timestamp=state["message_ts"],
            emoji="white_check_mark",
        )

        # Decide whether to polish: skip if draft is good (>= 80 chars and confident)
        needs_polish = (
            not response_text
            or len(response_text) < 80
            or confidence < 0.45
            or _looks_truncated(response_text)   # catch mid-sentence token cut-offs
        )

        # Guard: if confidence is very low AND no real draft, don't hallucinate.
        # Instead send a humble fallback rather than an LLM-generated guess.
        # if confidence < 0.15 and not response_text:
        #     response_text = (
        #         "I wasn't able to find specific information to address your question right now. "
        #         "Could you share any error messages or logs you're seeing? "
        #         "That'll help me point you in the right direction."
        #     )
        #     needs_polish = False

        if needs_polish:
            try:
                response_text = asyncio.run(_polish_answer(
                    draft=response_text,
                    message_text=message_text,
                    docs=docs_for_polish,
                    confidence=confidence,
                    problem_summary=problem_summary,
                ))
            except Exception as polish_err:
                logger.logger.warning(f"[SolutionProvider] Polish LLM call failed: {polish_err}. Sending fallback response.")
                # Fallback so the user always gets a response (e.g. 429 rate limit, API down)
                if research_files:
                    file_list = ", ".join(f"`{f.path}`" for f in research_files[:5])
                    response_text = (
                        f"Here are the relevant files we found for your question: {file_list}. "
                        "We couldn't generate a full summary right now (temporary issue — try again in a moment). "
                        "You can inspect these files for the answer."
                    )
                else:
                    response_text = (
                        "We couldn't generate a full answer right now (temporary issue). Please try again in a moment."
                    )

        # Low-confidence soft note
        if confidence < 0.6 and not state.get("doc_sufficient"):
            response_text += "\n\nIf this doesn't fully address your issue, let me know — I may need a bit more context."

        # Do not include relevant files in the response
        final_message = response_text.strip()
        state["response_text"] = final_message

        # Build Slack blocks (no file citations)
        blocks = slack_client.format_response_blocks(
            response_text=final_message,
            confidence=confidence,
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

        logger.logger.info(
            f"[SolutionProvider] sent answer confidence={confidence:.2f} len={len(final_message)}"
        )

        # Persist
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
                confidence=confidence,
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

    except Exception as e:
        logger.log_error(
            error_type="SolutionProviderError",
            error_message=str(e),
            user_id=user_id,
            channel_id=channel_id,
        )

    return state
