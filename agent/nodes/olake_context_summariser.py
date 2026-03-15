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


_PROMPT_TEMPLATE = """You are an intent classifier and strict extractor. You must output valid JSON only, with no markdown fences and no trailing explanation.

Inputs:
1) Current user question:
{user_question}

2) Conversation context (if any):
{conversation_context}

3) Full ABOUT_OLAKE document (multiple sections, separated by ==== headers):
{about_olake}

4) Repositories available for search (ABOUT_OLAKE_REPO_INFO). Each repo has a name and description of what it contains:
{about_olake_repo_info}

STEP 1 — Understand intent (REQUIRED):
- Decide whether the question NEEDS codebase search or is generic.
- GENERIC (no codebase search): Questions answerable from general OLake knowledge, docs, or ABOUT_OLAKE alone — e.g. "What is OLake?", "How does OLake handle CDC?", "What connectors does OLake support?", "How do I deploy OLake?". No need to search source code.
- NEEDS CODEBASE SEARCH: Questions that require looking at actual source code — e.g. "Where is snapshot expiry implemented?", "Which file defines the Kafka partition regex config?", "How is retention configured in the Postgres driver?", "Where do we log connection errors?". These need repo search.
- When in doubt, prefer needs_codebase_search true if the question mentions specific files, functions, config keys, or implementation details.

Output "needs_codebase_search": true or false (boolean).

STEP 2 — Only when needs_codebase_search is true:
- Identify which sections of ABOUT_OLAKE are relevant to answering the question and the conversation. Output that as "about_olake_relevant".
- For each repository that is most relevant, output an entry in "relevant_repos" with: (1) "name" — exact repo name (olake, olake-ui, olake-docs, olake-helm, olake-fusion), (2) "summary_points" — 3-5 concise bullet points describing what that repo contains and why it matters for the question, (3) "connections" — array of bullet-point strings, one per connected repo, each describing how this repo is connected to that other repo (e.g. "Olake backend executes ingestion jobs triggered by olake-ui's BFF via HTTP calls (check, discover, sync), and sends live stats back for UI monitoring."). One bullet per repo connection; omit or use [] if none. Keep each connection description concise.

When needs_codebase_search is false:
- Set "about_olake_relevant" to "" (empty string) and "relevant_repos" to [] (empty array). Do not extract sections or repos.

Rules (STRICT):
- Do NOT add any information. Do NOT infer or assume facts. Do NOT alter or paraphrase the content — use it verbatim.
- For about_olake_relevant: if the relevant portions are under ~1000 tokens (~4000 chars), include them in full; if longer, summarise to stay within the limit. Preserve facts verbatim; only shorten by removing tangential detail. Include only content from the ABOUT_OLAKE document. No preamble or commentary.
- Output valid JSON only. No ``` markdown. No trailing commas or text after the closing brace.

Output format (exactly):
{{"needs_codebase_search": true|false, "about_olake_relevant": "<extract or summary, or empty string if needs_codebase_search is false>", "relevant_repos": [{{"name": "olake", "summary_points": ["Core sync engine and drivers.", "Go + Java Iceberg writer.", "..."], "connections": ["Olake backend executes ingestion jobs triggered by olake-ui's BFF via HTTP (check, discover, sync); sends live stats back for UI monitoring.", "olake-helm deploys olake worker and UI as Kubernetes pods."]}}, {{"name": "olake-docs", "summary_points": ["User-facing docs.", "..."], "connections": []}}] or []}}
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
        needs_codebase_search = bool(parsed.get("needs_codebase_search", True))
        state["needs_codebase_search"] = needs_codebase_search

        if needs_codebase_search:
            relevant = (parsed.get("about_olake_relevant") or "").strip()
            state["about_olake_summary"] = relevant if relevant else ABOUT_OLAKE.strip()
            repos = parsed.get("relevant_repos")
            if isinstance(repos, list) and repos:
                names = []
                detail = []
                for r in repos:
                    if isinstance(r, dict) and r.get("name"):
                        names.append(str(r["name"]).strip())
                        detail.append({
                            "name": str(r["name"]).strip(),
                            "summary_points": r.get("summary_points") if isinstance(r.get("summary_points"), list) else [],
                            "connections": r.get("connections") if isinstance(r.get("connections"), list) else [],
                        })
                    elif isinstance(r, str) and r.strip():
                        names.append(r.strip())
                        detail.append({"name": r.strip(), "summary_points": [], "connections": []})
                state["relevant_repos"] = names if names else ["olake"]
                state["relevant_repos_detail"] = detail if detail else []
            else:
                state["relevant_repos"] = ["olake"]
                state["relevant_repos_detail"] = []
        else:
            state["about_olake_summary"] = ""
            state["relevant_repos"] = []
            state["relevant_repos_detail"] = []
    except Exception:
        state["needs_codebase_search"] = True
        state["about_olake_summary"] = ABOUT_OLAKE.strip()
        state["relevant_repos"] = ["olake"]
        state["relevant_repos_detail"] = []

    return state
