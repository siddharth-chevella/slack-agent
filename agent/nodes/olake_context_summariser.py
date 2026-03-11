"""
OLake Context Summariser Node — Produces a focused, token-efficient excerpt of ABOUT_OLAKE.

Takes the user question, optional conversation context, and full ABOUT_OLAKE;
returns JSON with only the sections relevant to the query, summarised if >~1k tokens
else unchanged. No additions, no alterations, no assumptions — strict extract/summarise only.
"""

from __future__ import annotations
import asyncio
import json
import re
from typing import Any, Dict, List

from agent.state import ConversationState
from agent.llm import get_chat_completion
from agent.config import ABOUT_OLAKE, ABOUT_OLAKE_REPO_INFO


_JSON_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)


def _parse_json(text: str | None) -> dict:
    if not text:
        raise ValueError("LLM returned empty response")
    text = text.strip()
    text = _JSON_FENCE.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Truncation repair
    if not text.rstrip().endswith("}"):
        text = text.rstrip()
        if text.count('"') % 2 != 0:
            text += '"'
        text += "}"
    return json.loads(text)


def _conversation_summary(thread_context: List[Dict[str, Any]], max_messages: int = 10) -> str:
    """Build a short summary of previous messages for the prompt."""
    if not thread_context:
        return ""
    lines = []
    for msg in thread_context[-max_messages:]:
        role = "assistant" if msg.get("is_bot") else "user"
        text = (msg.get("text") or msg.get("content") or "").strip()
        if text:
            lines.append(f"{role}: {text[:200]}" + ("..." if len(text) > 200 else ""))
    if not lines:
        return ""
    return "Previous conversation (recent messages):\n" + "\n".join(lines)


_PROMPT_TEMPLATE = """You are a strict extractor. You must output valid JSON only, with no markdown fences and no trailing explanation.

Inputs:
1) Current user question:
{user_question}

2) Conversation context (if any):
{conversation_context}

3) Full ABOUT_OLAKE document (multiple sections, separated by ==== headers):
{about_olake}

4) Repositories available for search (ABOUT_OLAKE_REPO_INFO). Each repo has a name and description of what it contains:
{about_olake_repo_info}

Task:
- Identify which sections of ABOUT_OLAKE are relevant to answering the current question and the conversation. Output that as "about_olake_relevant".
- Identify which repository/repositories are most relevant to the question (e.g. olake for core/sync/connectors, olake-docs for documentation, olake-ui for UI/BFF, olake-helm for Kubernetes, olake-fusion for Iceberg table management/expiry). Output their names as "relevant_repos" (JSON array of strings, e.g. ["olake", "olake-docs"]). Use the exact repo names as they appear in the headers (olake, olake-ui, olake-docs, olake-helm, olake-fusion).

Rules (STRICT):
- Do NOT add any information. Do NOT infer or assume facts. Do NOT alter or paraphrase the content — use it verbatim.
- If the relevant ABOUT_OLAKE portions together are under ~1000 tokens (approximately 4000 characters), include them in full, unchanged.
- If the relevant portions exceed that, summarise only those sections to stay within the limit. When summarising, preserve facts verbatim where possible; only shorten by removing tangential detail. Do not add new content.
- Include only content from the ABOUT_OLAKE document in "about_olake_relevant". No preamble, no "based on the document", no extra commentary.
- Output valid JSON only. No ``` markdown. No trailing commas or text after the closing brace.

Output format (exactly):
{{"about_olake_relevant": "<extract or summary of relevant ABOUT_OLAKE sections here>", "relevant_repos": ["repo1", "repo2"]}}
"""


def summarise_olake_context(state: ConversationState) -> ConversationState:
    """
    Run the OLake context summariser: produce a focused excerpt of ABOUT_OLAKE
    and set state["about_olake_summary"].
    """
    user_question = (state.get("message_text") or "").strip()
    if not user_question:
        state["about_olake_summary"] = ABOUT_OLAKE.strip()
        return state

    thread_context = state.get("thread_context") or []
    conversation_context = _conversation_summary(thread_context)
    if not conversation_context.strip():
        conversation_context = "(No previous messages in this thread.)"

    prompt = _PROMPT_TEMPLATE.format(
        user_question=user_question,
        conversation_context=conversation_context,
        about_olake=ABOUT_OLAKE.strip(),
        about_olake_repo_info=(ABOUT_OLAKE_REPO_INFO or "").strip(),
    )

    try:
        response = asyncio.run(
            get_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
            )
        )
        parsed = _parse_json(response)
        relevant = (parsed.get("about_olake_relevant") or "").strip()
        state["about_olake_summary"] = relevant if relevant else ABOUT_OLAKE.strip()
        repos = parsed.get("relevant_repos")
        if isinstance(repos, list) and repos:
            state["relevant_repos"] = [str(r).strip() for r in repos if str(r).strip()]
        else:
            state["relevant_repos"] = ["olake"]
    except Exception:
        state["about_olake_summary"] = ABOUT_OLAKE.strip()
        state["relevant_repos"] = ["olake"]

    return state
