"""
Deep Researcher Node — Optimized for ripgrep + ast-grep (no vector/semantic search).

Alex, a senior OLake support engineer, plans and runs searches via SearchParams.
The model returns search_params (array of param dicts); each is passed to
CodebaseSearchEngine.search() which runs both ripgrep and ast-grep and returns
deduplicated results.
"""

from __future__ import annotations
import asyncio
import json
import logging
import sys
import traceback
from typing import Dict, Any, List, Optional
from pathlib import Path

from agent.state import ConversationState, ResearchFile
from agent.llm import get_chat_completion
from agent.logger import get_logger
from agent.config import Config, ABOUT_OLAKE, ABOUT_OLAKE_REPO_INFO
from agent.codebase_search import CodebaseSearchEngine, SearchParams
from agent.utils.parser import parse_llm_json, parse_summarizer_json, parse_planner_json
from agent.persistence import get_database

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System Prompt — Optimized for ripgrep + ast-grep (no vector/semantic search)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """You are Alex, a senior support engineer at OLake/Datazip. Think and reason explicitly in first person: use "I" in your reasoning (e.g. "I need to search for ...", "I will look for ...", "I should target ...").

About OLake:
{about_olake}

Repositories available for search (each is searched individually; use this to target the right repo):
{about_olake_repo_info}

Your work so far and what you learned: {previous_step_reasoning}

Your job: Research the codebase using ONE search tool with standardized parameters (SearchParams). There is NO vector search, NO semantic search, NO keyword embedding. Results come only from literal text matches and structural code patterns.

PREFER REGEX, ONE SEARCH PER REPO: Use regex as the pattern to match all related terms in a single search per repo instead of multiple iterations on the same repo. Combine identifiers with | (OR), e.g. "ExpireSnapshot|expireSnapshot|ExpireSnapshots|retention", so one SearchParams per repo gathers all relevant hits at once. Avoid running several separate searches on the same repo—express everything you need for that repo in one regex pattern.

HOW THE TOOL WORKS:
The search tool accepts a SearchParams object and routes internally to either ripgrep (text/regex) or ast-grep (code structure). You control which by how you phrase the pattern:
- REGEX patterns (preferred): Use regex to match multiple variants in one go. Examples: "ExpireSnapshot|expireSnapshot|ExpireSnapshots", "snapshot\\.retention|retention_days", "topic.*retention|topic_retention", "func.*Snapshot|type.*Snapshot". Ripgrep supports full regex; use it to cover a whole topic in one search per repo.
- STRUCTURAL patterns: code-like snippets for definitions/usages when needed. Use for: "func ExpireSnapshot" (Go), "type SnapshotManager". Primary language for OLake is Go.

SearchParams fields:
- pattern: the search string (literal, regex, or structural code snippet)
- repo: which repo to search ("olake", "olake-ui", "olake-docs", "olake-helm", "olake-fusion")
- max_results: cap on results (default 50)
- file_types: list of extensions to filter, e.g. ["go"], ["go", "yaml"], ["md"]
- lang: language hint for structural/ast search (e.g. "go"); if None, inferred from file_types
- exclude_dirs: list of dirs to skip, e.g. ["vendor", "testdata"]
- context_lines: lines of context around each match (default 2)

GO/OLAKE NAMING — CRITICAL: OLake is written in Go. Go uses PascalCase for exported types/functions (ExpireSnapshot, DeleteSnapshots) and camelCase for unexported (expireSnapshot). Python-style snake_case will NOT match. Always use patterns that could literally appear in Go code: "ExpireSnapshot", "expireSnapshot", "Snapshot", "retention". Emit both PascalCase and camelCase variants when unsure.

PATTERN QUALITY RULES (apply to all patterns regardless of tool):
- Use compound terms over single words: "partition_regex" not "partition", "topic.*retention" not "retention"
- Weak patterns match many unrelated areas; strong patterns stay domain-relevant
- Examples of weak vs strong:
    WEAK "partition" → matches Kafka partitions, drivers, tests, unrelated code
    STRONG "PartitionRegex", "partition_regex" → scoped to the actual feature

    WEAK "retention" → matches snapshot retention, logs, many modules
    STRONG "topic.*retention", "topic_retention" → scoped to topic config

    WEAK "lag" → matches any lag (network, replication, generic)
    STRONG "wal.*lag", "pgoutput.*lag", "replication.*lag" → scoped to CDC/Postgres

STRATEGY:
- Use regex to combine related terms (identifiers, config keys, error strings) into one pattern per repo so a single search covers the topic.
- Prefer one SearchParams per repo with a regex like "TermA|termA|TermB|termB" over multiple SearchParams on the same repo.
- Use SEARCH HISTORY to avoid repeating failed searches.
- If the user asks "how do I X?", think: what function or config implements X? Put all related names in one regex for that repo.
- If the user asks "where is Y?", use Y and related identifiers (YManager, NewY) in one regex per repo.

OUTPUT FORMAT (valid JSON only, no markdown fences):
{{
  "result": [
    {{
      "thinking": "First-person work so far: e.g. I have the key pieces of behavior for the question, but the specific function/config that completes the explanation is still missing, so I will now search for the missing pattern Q.",
      "search_intent": "One or two lines: what you are searching and why (can use I).",
      "search_params": [
        {{
          "pattern": "ExpireSnapshot|expireSnapshot|ExpireSnapshots|retention",
          "repo": "olake",
          "file_types": ["go"],
          "lang": "go",
          "exclude_dirs": null,
          "context_lines": 2
        }}
      ],
      "is_conceptual": false
    }}
  ]
}}

RULES:
- search_intent is REQUIRED every time: 1-2 lines on what you're searching and why.
- Prefer richer regex so one pattern covers many related matches per repo — e.g. "(snapshot|retention)[_.]?\\w*\\s*[:=]" matches config keys (snapshot_retention, retention_days, snapshot.retention) at assignment or definition sites in one search; use grouping, optional parts ([_.]?), and character classes (\\w, \\s) to capture variants without listing every token. Max 5 SearchParams per iteration (ideally one per repo).
- Patterns must be valid regex or structural code snippets. No full sentences or questions.
- If you already have sufficient findings, output empty search_params [] to signal readiness to evaluate.
- Set is_conceptual to true only for general-knowledge questions (e.g. "What is OLake?") that need no code search; then leave search_params empty.
"""


def _system_prompt(previous_step_reasoning: Optional[str] = None) -> str:
    return _SYSTEM_PROMPT_TEMPLATE.format(about_olake=ABOUT_OLAKE, about_olake_repo_info=ABOUT_OLAKE_REPO_INFO.strip(), previous_step_reasoning=previous_step_reasoning or "This is a new conversation. No previous-step reasoning exists.")

# Prompt for file summarization (LLM summarizes each file; no truncation).
# The files are passed to this prompt in the USER message: for each file we send path, pattern used for retrieval, and full content (see _build_files_summary).
_SUMMARIZE_FILES_SYSTEM = """You summarize codebase files so an evaluator can decide if enough context exists to answer a user question.

About OLake:
{about_olake}

Repositories (for context):
{about_olake_repo_info}

RULES:
1. Understand the USER QUESTION intent first. Use it to decide whether each file is relevant. If a file is irrelevant to the question, do NOT include it in your output (skip it).
2. For each relevant file, output file_path and a concise descriptive summary in bullet points. Example format:
   - What the file does (e.g. "Validates JWT tokens using Clerk (jose library)")
   - Key behavior (e.g. "Middleware applied globally except /public/* routes")
   - Important details (e.g. "On failure: returns 401, logs to Sentry with user_id")
   - Dependencies if clear (e.g. "Depends on: config/env.py (CLERK_SECRET_KEY)")
3. Every sentence MUST be grounded in the file content or the question. Do NOT guess or make up information. If you are unsure about something, state it clearly (e.g. "Unclear whether...").
4. You receive the full file content (no truncation). Base your summary only on what is actually in the file.

OUTPUT FORMAT: Return ONLY a single JSON object. No markdown, no trailing backticks, no explanation before or after. Valid JSON only:
{{"summaries": [{{"file_path": "<path>", "summary": "<bullet summary>"}}, ...]}}

Omit any file that is irrelevant to the user question from the summaries array."""

# Prompt for post-search evaluation: analyze retrieved data and decide if enough to answer
_EVALUATE_PROMPT = """You are evaluating whether the retrieved codebase context is enough to answer the user's question accurately.

Understand the intent behind the user question. Carefully and objectively analyze the files found. Focus on what the retrieved files are about and how they relate to the user question (if they are relevant).

USER QUESTION: "{user_query}"

FILES FOUND:
{files_found}

Analyze the retrieved data: Does it contain the right modules, config, or docs to answer the question? If key info is missing or the files are from the wrong domain, say CONTINUE so the agent can search again with a better target.

OUTPUT FORMAT: Return ONLY a single JSON object. No markdown, no trailing backticks, no explanation before or after. Valid JSON only:
{{ "reason": "First-person reason that starts with \"I ...\" (one-two sentences why).", "decision": "CONTINUE" | "DONE" }}

- DONE: I have enough (right area, relevant code/config) to answer accurately.
- CONTINUE: I need more (missing key files, wrong domain, or too few relevant matches).

Example 1: {{"reason":"I found the exact implementation and config wiring for the behavior that answers the user's question, so the retrieved files are sufficient to answer accurately.","decision":"DONE"}}

Example 2: {{"reason":"I found related modules, but the specific function or config that determines the behavior can answer the user's issue is missing from the retrieved files, so I need to search again.","decision":"CONTINUE"}}
"""

# Summarizer: max total content chars per LLM call (avoid context overflow); batch size when batching
_SUMMARIZE_MAX_CHARS = 80_000
_SUMMARIZE_BATCH_SIZE = 5

def _dict_to_search_params(d: Dict[str, Any], default_repo: str = "olake") -> SearchParams:
    """Build SearchParams from LLM response dict (search_params item)."""
    return SearchParams(
        pattern=str(d.get("pattern", "")).strip() or ".",
        repo=d.get("repo") or default_repo,
        max_results=int(d.get("max_results", 50)),
        file_types=d.get("file_types"),
        lang=d.get("lang"),
        exclude_dirs=d.get("exclude_dirs"),
        context_lines=int(d.get("context_lines", 2)),
    )


# ---------------------------------------------------------------------------
# Deep Researcher Node
# ---------------------------------------------------------------------------

class DeepResearcher:
    """
    Alex - Senior OLake Support Engineer

    Thinks out loud while researching the codebase iteratively.
    Returns all gathered context with retrieval reasons.
    Uses a single CodebaseSearchEngine; repo is specified per search via SearchParams.
    """

    def __init__(self):
        self.search_engine = CodebaseSearchEngine()
        self.max_iterations = 7
    
    def _build_files_summary(self, user_query: str, files: List[ResearchFile]) -> str:
        """Build a summary of files for the evaluator via LLM: path + concise descriptive summary per file. No truncation."""
        if not files:
            return "(No files yet.)"
        about_olake = (ABOUT_OLAKE or "").strip()
        about_olake_repo_info = (ABOUT_OLAKE_REPO_INFO or "").strip()
        system_prompt = _SUMMARIZE_FILES_SYSTEM.format(
            about_olake=about_olake,
            about_olake_repo_info=about_olake_repo_info,
        )
        # Process all files: batch only by context size so we never drop files (order may put important ones later)
        batches: List[List[ResearchFile]] = []
        current_batch: List[ResearchFile] = []
        current_chars = 0
        for f in files:
            file_chars = len((f.content or "").strip())
            if current_batch and current_chars + file_chars > _SUMMARIZE_MAX_CHARS:
                batches.append(current_batch)
                current_batch = []
                current_chars = 0
            current_batch.append(f)
            current_chars += file_chars
        if current_batch:
            batches.append(current_batch)
        all_summaries: List[Dict[str, Any]] = []
        for batch in batches:
            user_parts = [f"USER QUESTION: {user_query}", ""]
            for f in batch:
                pattern = getattr(f, "search_pattern", None) or ""
                user_parts.append(f"--- FILE: {f.path} ---")
                user_parts.append(f"Pattern used for retrieval: {pattern}")
                user_parts.append("")
                user_parts.append((f.content or "").strip())
                user_parts.append("")
            user_message = "\n".join(user_parts)
            try:
                response = asyncio.run(
                    get_chat_completion(
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message},
                        ],
                        temperature=0.2,
                    )
                )
                parsed = parse_summarizer_json(response)
                all_summaries.extend(parsed)
            except Exception as e:
                log.warning(f"[DeepResearcher] Summarizer LLM failed: {e}")
                for f in batch:
                    pattern = getattr(f, "search_pattern", None) or ""
                    all_summaries.append({"file_path": f.path, "summary": f"(Pattern: {pattern}). Summarization failed."})
        if not all_summaries:
            for f in files:
                pattern = getattr(f, "search_pattern", None) or ""
                all_summaries.append({"file_path": f.path, "summary": f"Pattern used: {pattern}"})
        lines = []
        for i, item in enumerate(all_summaries, 1):
            path = item.get("file_path", "")
            summary = (item.get("summary", "") or "").strip()
            lines.append(f"{i}. {path}\n   {summary}")
        return "\n\n".join(lines) if lines else "(No files yet.)"

    def _evaluate_context(
        self,
        user_query: str,
        files: List[ResearchFile],
    ) -> Dict[str, Any]:
        """
        Ask LLM whether we have enough context. Returns a dictionary with "decision" and "reason".
        """
        if not files:
            return {"decision": "CONTINUE", "reason": "No files yet; need to search."}
        prompt = _EVALUATE_PROMPT.format(
            user_query=user_query,
            files_found=files,
        )
        try:
            response = asyncio.run(get_chat_completion(
                messages=[
                    {"role": "system", "content": "You output only valid JSON: {\"decision\": \"CONTINUE\" or \"DONE\", \"reason\": \"one sentence\"}. No markdown."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            ))
            parsed = parse_llm_json(response)
            if "error" in parsed:
                log.warning(f"[DeepResearcher] Evaluate step failed: {parsed['error']}, defaulting to CONTINUE")
                return {"decision": "CONTINUE", "reason": f"Model returned invalid JSON: {parsed['error']}"}
            return parsed
        except Exception as e:
            log.warning(f"[DeepResearcher] Evaluate step failed: {e}, defaulting to CONTINUE")
            return {"decision": "CONTINUE", "reason": f"Evaluation failed: {e}"}
    def __call__(self, state: ConversationState) -> ConversationState:
        """Main entry point for the deep researcher node."""

        files_list: List[ResearchFile] = []

        logger = get_logger()
        user_query = state["user_query"]

        conversation_history = state["thread_context"] or "No conversation history found. This is a fresh conversation."

        iteration = 1

        try:
            while iteration <= self.max_iterations:
                print("Current iteration: ", iteration)
                # Build prompt for LLM

                _emit({"type": "thinking_start", "iteration": iteration + 1})
                about_olake = (state.get("about_olake_summary") or ABOUT_OLAKE).strip()
                relevant = state.get("relevant_repos") or []
                if isinstance(relevant, list) and relevant:
                    repo_hint = "For this question, prefer searching these repos first: " + ", ".join(relevant) + ".\n\n"
                    repo_info_for_prompt = repo_hint + (ABOUT_OLAKE_REPO_INFO or "").strip()
                else:
                    repo_info_for_prompt = (ABOUT_OLAKE_REPO_INFO or "").strip()
                system_prompt = _system_prompt()

                system_prompt = 

                # Get LLM response (single non-streaming call)
                response = asyncio.run(get_chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                ))

                # Parse response: search_intent, thinking, patterns, is_conceptual
                result = parse_planner_json(response)
                if "error" in result:
                    log.warning(f"[DeepResearcher] Planner JSON parsing failed: {result['error']}")
                   
                    return {
                        "research_done": True,
                        "research_error": True,
                        "response_text": "I wasn't able to find enough information to answer your question.",
                    }

                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = []
                    for sp in result["search_params"]:
                        futures.append(executor.submit(self.search_engine.search, sp))
                    results = [future.result() for future in futures]
                    all_files.extend(results)

                # Deduplicate by path and merge into all_files; track newly added for history/emit
                existing_paths = {f.path for f in all_files}
                unique_files = []
                for f in all_files:
                    if f.file_path not in existing_paths:
                        existing_paths.add(f.file_path)
                        unique_files.append(f)

                if unique_files:
                    decision, eval_reason = self._evaluate_context(user_query, unique_files)
                    state["eval_reason"] = eval_reason
                    logger.logger.info(f"[DeepResearcher] Evaluate: {decision} — {eval_reason}")
                    if decision == "DONE":
                        state["research_done"] = True
                        break

                iteration += 1
                
                files_list.extend(unique_files)


# Synchronous wrapper for LangGraph
def deep_researcher(state: ConversationState) -> ConversationState:
    """Synchronous wrapper for the DeepResearcher node."""
    researcher = DeepResearcher()
    return researcher(state)
