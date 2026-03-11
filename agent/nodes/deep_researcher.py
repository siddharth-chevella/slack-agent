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
import re
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
from agent.persistence import get_database

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System Prompt — Optimized for ripgrep + ast-grep (no vector/semantic search)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """You are Alex, a senior support engineer at OLake/Datazip.

About OLake:
{about_olake}

Repositories available for search (each is searched individually; use this to target the right repo):
{about_olake_repo_info}

Your job: Research the codebase using ONE search tool with standardized parameters (SearchParams). There is NO vector search, NO semantic search, NO keyword embedding. Results come only from literal text matches and structural code patterns.

HOW THE TOOL WORKS:
The search tool accepts a SearchParams object and routes internally to either ripgrep (text/regex) or ast-grep (code structure). You control which by how you phrase the pattern:
- TEXT/REGEX patterns: literal strings or regex that would appear in source files. Use for: identifiers (ExpireSnapshot, expire_snapshots), config keys (snapshot.retention), error strings, package names, comments.
- STRUCTURAL patterns: code-like snippets for finding definitions/usages. Use for: "func ExpireSnapshot" (Go), "type SnapshotManager", "class Foo". Primary language for OLake is Go.

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
    STRONG "partition_regex", "partition regex" → scoped to the actual feature

    WEAK "retention" → matches snapshot retention, logs, many modules
    STRONG "topic.*retention", "topic_retention" → scoped to topic config

    WEAK "lag" → matches any lag (network, replication, generic)
    STRONG "wal.*lag", "pgoutput.*lag", "replication.*lag" → scoped to CDC/Postgres

STRATEGY:
- Start with 2-4 patterns targeting identifiers, config keys, or error strings from the question.
- Add 1-2 structural patterns (func/type definitions) to find implementations.
- Use SEARCH HISTORY to avoid repeating failed searches.
- If the user asks "how do I X?", think: what function or config implements X?
- If the user asks "where is Y?", use Y and related identifiers (YManager, NewY).

OUTPUT FORMAT (valid JSON only, no markdown fences):
{{
  "thinking": "Your reasoning: what you need and why these patterns",
  "search_intent": "One or two lines: what you are searching and why.",
  "search_params": [
    {{
      "pattern": "ExpireSnapshot",
      "repo": "olake",
      "max_results": 50,
      "file_types": ["go"],
      "lang": "go",
      "exclude_dirs": null,
      "context_lines": 2
    }}
  ],
  "problem_summary": "optional one-sentence restatement of the user question",
  "is_conceptual": false
}}

RULES:
- search_intent is REQUIRED every time: 1-2 lines on what you're searching and why.
- Max 5 SearchParams objects per iteration.
- Patterns must be strings that could literally appear in source (text) or structural code snippets (ast-style). No full sentences or questions.
- If you already have sufficient findings, output empty search_params [] to signal readiness to evaluate.
- Set is_conceptual to true only for general-knowledge questions (e.g. "What is OLake?") that need no code search; then leave search_params empty.
"""


def _system_prompt(about_olake: str, about_olake_repo_info: str = "") -> str:
    repo_info = (about_olake_repo_info or ABOUT_OLAKE_REPO_INFO or "").strip()
    return _SYSTEM_PROMPT_TEMPLATE.format(about_olake=about_olake, about_olake_repo_info=repo_info)

# Prompt for post-search evaluation: analyze retrieved data and decide if enough to answer
_EVALUATE_PROMPT = """You are evaluating whether the retrieved codebase context is enough to answer the user's question accurately.

USER QUESTION: "{message_text}"

FILES FOUND (path, why retrieved, and a snippet):
{files_summary}


Analyze the retrieved data: Does it contain the right modules, config, or docs to answer the question? If key info is missing or the files are from the wrong domain, say CONTINUE so the agent can search again with a better target.

Reply with JSON only (no markdown):
{{ "reason": "One sentence why.", "decision": "CONTINUE" | "DONE" }}

- DONE: I have enough (right area, relevant code/config) to answer accurately.
- CONTINUE: I need more (missing key files, wrong domain, or too few relevant matches).
"""


# ---------------------------------------------------------------------------
# JSON Parser with Fallbacks
# ---------------------------------------------------------------------------

_PARSE_RE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)


def _parse_json(text: str | None) -> dict:
    """Parse LLM JSON with progressive fallbacks for truncated responses."""
    if not text:
        raise ValueError("LLM returned empty response")
    text = text.strip()
    text = _PARSE_RE_FENCE.sub("", text).strip()

    # 1. Happy path
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Truncation repair
    repaired = text
    if not repaired.endswith("}"):
        open_strings = repaired.count('"') % 2
        if open_strings:
            repaired += '"'
        repaired += '}'
    try:
        return json.loads(repaired)
    except json.JSONDecodeError:
        pass

    # 3. Regex extraction as last resort (for evaluate step or researcher response)
    result = {}
    for field, pattern in [
        ("decision", r'"decision"\s*:\s*"(CONTINUE|DONE)"'),
        ("reason", r'"reason"\s*:\s*"([^"]*)"'),
        ("confidence", r'"confidence"\s*:\s*([0-9.]+)'),
        ("thinking", r'"thinking"\s*:\s*"([^"]*)"'),
        ("search_intent", r'"search_intent"\s*:\s*"((?:[^"\\]|\\.)*)"'),
        ("problem_summary", r'"problem_summary"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    ]:
        m = re.search(pattern, text)
        if m:
            val = m.group(1)
            result[field] = float(val) if field == "confidence" else val

    if "decision" in result:
        result.setdefault("confidence", 0.4)
        result.setdefault("thinking", "[truncated]")
        result.setdefault("search_queries", [])
        return result

    # 4. Truncated researcher response: we have "thinking" and/or "search_intent" but no valid JSON — return partial so the loop can continue (e.g. fallback search)
    if "thinking" in result or "search_intent" in result:
        result.setdefault("thinking", "(response truncated)")
        result.setdefault("search_intent", "Searching for relevant code.")
        result.setdefault("search_params", [])
        result.setdefault("problem_summary", "")
        return result

    raise ValueError(f"Cannot parse LLM JSON even after repair: {text[:200]!r}")


# ---------------------------------------------------------------------------
# Deep Researcher Node
# ---------------------------------------------------------------------------

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


class DeepResearcher:
    """
    Alex - Senior OLake Support Engineer

    Thinks out loud while researching the codebase iteratively.
    Returns all gathered context with retrieval reasons.
    Uses a single CodebaseSearchEngine; repo is specified per search via SearchParams.
    """

    def __init__(self):
        self.search_engine = CodebaseSearchEngine()
        self.max_iterations = Config.MAX_RESEARCH_ITERATIONS
        self.max_files = Config.MAX_CONTEXT_FILES
        self.min_confidence = Config.MIN_CONFIDENCE_TO_STOP
    
    def _build_user_prompt(self, state: ConversationState) -> str:
        """Build the user prompt: raw message + search history + files found so far."""
        message_text = state["message_text"]
        search_history = state.get("search_history", [])
        research_files = state.get("research_files", [])
        iteration = state.get("research_iterations", 0)
        
        # Search history: what was searched, why, and what was found (so the agent knows what to try next and what to avoid)
        history_block = ""
        if search_history:
            history_block = "\n\nSEARCH HISTORY (what was searched, why, and what was found — use this to plan the next search and avoid repeating):\n" + "\n".join(
                f"  {i+1}. {entry}" for i, entry in enumerate(search_history)
            )
        
        # Files found so far: path, reason, and short snippet so the agent can judge relevance
        files_context = ""
        if research_files:
            files_context = "\n\nFILES FOUND SO FAR:\n"
            for i, f in enumerate(research_files[:12], 1):
                snippet = ((f.content or "").strip()[:180].replace("\n", " "))
                files_context += f"  {i}. {f.path}\n     Why: {f.retrieval_reason}\n     Snippet: {snippet}...\n"
            if len(research_files) > 12:
                files_context += f"  ... and {len(research_files) - 12} more\n"
        
        return f"""USER MESSAGE: "{message_text}"
{history_block}

CURRENT RESEARCH ITERATION: {iteration}/{self.max_iterations}
FILES FOUND: {len(research_files)}
{files_context}

Analyze the question and the search history. Decide what to search next (or output empty patterns if we have enough to evaluate). Output JSON with search_intent, thinking, and patterns."""

    def _build_files_summary(self, files: List[ResearchFile], max_per_file: int = 400) -> str:
        """Build a summary of files for the evaluator: path, reason, and content snippet."""
        lines = []
        for i, f in enumerate(files[:15], 1):
            snippet = (f.content or "").strip()[:max_per_file].replace("\n", " ")
            lines.append(f"{i}. {f.path}\n   Why: {f.retrieval_reason}\n   Snippet: {snippet}...")
        return "\n\n".join(lines) if lines else "(No files yet.)"

    def _evaluate_context(
        self,
        message_text: str,
        files: List[ResearchFile],
    ) -> tuple[str, str]:
        """
        Ask LLM whether we have enough context. Returns (decision, reason).
        decision is CONTINUE or DONE.
        """
        if not files:
            return "CONTINUE", "No files yet; need to search."
        summary = self._build_files_summary(files)
        prompt = _EVALUATE_PROMPT.format(
            message_text=message_text,
            files_summary=summary,
        )
        try:
            response = asyncio.run(get_chat_completion(
                messages=[
                    {"role": "system", "content": "You output only valid JSON: {\"decision\": \"CONTINUE\" or \"DONE\", \"reason\": \"one sentence\"}. No markdown."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
            ))
            parsed = _parse_json(response)
            print("Evaluate context response:", response)
            raw = (parsed.get("decision") or "CONTINUE").upper()
            decision = "DONE" if raw == "DONE" else "CONTINUE"
            reason = parsed.get("reason", "") or "Evaluation complete."
            return decision, reason
        except Exception as e:
            log.warning(f"Evaluate step failed: {e}, defaulting to CONTINUE")
            return "CONTINUE", "Evaluation failed; continuing to be safe."

    def _calculate_confidence(self, files: List[ResearchFile], iteration: int) -> float:
        """Calculate confidence score based on gathered files."""
        if not files:
            return 0.1  # Very low confidence if no files found

        # Base confidence from file count
        count_score = min(1.0, len(files) / 5.0)  # 5 files = 1.0

        # Average relevance score
        avg_relevance = sum(f.relevance_score for f in files) / len(files)

        # Diversity bonus (different file paths)
        unique_dirs = len(set(Path(f.path).parent for f in files))
        diversity_score = min(1.0, unique_dirs / 3.0)

        # Combine with weights
        confidence = (
            count_score * 0.5 +  # Increased weight for file count
            avg_relevance * 0.3 +
            diversity_score * 0.2
        )

        # Boost slightly with more iterations (we've tried more things)
        iteration_bonus = min(0.1, iteration * 0.02)

        return min(1.0, confidence + iteration_bonus)
    
    def __call__(self, state: ConversationState) -> ConversationState:
        """Main entry point for the deep researcher node."""
        logger = get_logger()
        message_text = state["message_text"]

        iteration = state.get("research_iterations", 0)
        thinking_log = state.get("thinking_log", [])
        search_history = list(state.get("search_history", []))
        all_files = list(state.get("research_files", []))
        eval_reason = ""

        # Conceptual check: set by previous run or by this run's first LLM response
        is_conceptual = state.get("is_conceptual", False)
        if is_conceptual:
            logger.logger.info("[DeepResearcher] Conceptual question detected, skipping code search")
            state["research_confidence"] = 0.6
            state["research_iterations"] = 0
            state["final_confidence"] = 0.6
            state["doc_sufficient"] = True
            return state

        # Optional progress callback for CLI live display (state may have _cli_progress_callback)
        emit = state.get("_cli_progress_callback")
        if callable(emit):
            def _emit(ev: dict) -> None:
                try:
                    emit(ev)
                except Exception:
                    pass
        else:
            def _emit(ev: dict) -> None:
                pass

        try:
            while iteration < self.max_iterations:
                print("Current iteration: ", iteration)
                # Build prompt for LLM
                user_prompt = self._build_user_prompt(state)

                _emit({"type": "thinking_start", "iteration": iteration + 1})
                about_olake = (state.get("about_olake_summary") or ABOUT_OLAKE).strip()
                relevant = state.get("relevant_repos") or []
                if isinstance(relevant, list) and relevant:
                    repo_hint = "For this question, prefer searching these repos first: " + ", ".join(relevant) + ".\n\n"
                    repo_info_for_prompt = repo_hint + (ABOUT_OLAKE_REPO_INFO or "").strip()
                else:
                    repo_info_for_prompt = (ABOUT_OLAKE_REPO_INFO or "").strip()
                system_prompt = _system_prompt(about_olake, repo_info_for_prompt)

                # Get LLM response (single non-streaming call)
                response = asyncio.run(get_chat_completion(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                ))

                # Parse response: search_intent, thinking, patterns, optional problem_summary, is_conceptual
                result = _parse_json(response)

                thinking = result.get("thinking", "")
                search_intent = (result.get("search_intent") or "").strip() or "Searching for relevant code."

                preview = (thinking[:80] + "…") if len(thinking) > 80 else thinking
                _emit({"type": "thinking_done", "iteration": iteration + 1, "thinking": thinking, "preview": preview})
                _emit({"type": "search_intent", "text": search_intent})
                if result.get("problem_summary"):
                    state["problem_summary"] = (result.get("problem_summary") or "").strip()
                if "is_conceptual" in result:
                    state["is_conceptual"] = bool(result.get("is_conceptual", False))

                search_params_list = result.get("search_params", []) or []
                if not isinstance(search_params_list, list):
                    search_params_list = [search_params_list] if search_params_list else []

                # Conceptual: skip code search
                if state.get("is_conceptual", False) and not all_files:
                    logger.logger.info("[DeepResearcher] Conceptual question; skipping code search")
                    state["research_confidence"] = 0.6
                    state["research_done"] = True
                    state["final_confidence"] = 0.6
                    state["doc_sufficient"] = True
                    break

                default_repo = "olake"
                if isinstance(state.get("relevant_repos"), list) and state["relevant_repos"]:
                    default_repo = state["relevant_repos"][0]

                # Force at least one search when we have no files yet (derive from message_text)
                if not search_params_list and len(all_files) == 0:
                    filler = {"hi", "team", "is", "it", "to", "in", "the", "a", "an", "and", "or", "can", "how", "what", "possible", "please", "help", "thanks"}
                    raw_words = [w.strip(".,?!") for w in message_text.strip().split() if w.strip()]
                    words = [w for w in raw_words if len(w) > 2 and w.lower() not in filler][:5]
                    go_terms = []
                    for w in words:
                        if w.endswith("s") and len(w) > 3 and w.isalpha():
                            go_terms.append(w[0].upper() + w[1:-1])
                    patterns = (words + go_terms)[:6] or ["config", "documentation"]
                    search_params_list = [
                        {"pattern": p, "repo": default_repo, "max_results": 50, "file_types": ["go"], "lang": "go"}
                        for p in patterns
                    ]
                    log.info(f"[DeepResearcher] No search_params from LLM; forcing from message: {[p['pattern'] for p in search_params_list]}")

                # Log thinking and search intent
                thinking_entry = f"Iteration {iteration + 1}: {thinking}"
                thinking_log.append(thinking_entry)
                state["thinking_log"] = thinking_log

                logger.logger.info(f"[DeepResearcher] {search_intent}")
                logger.logger.info(f"[DeepResearcher] search_params count={len(search_params_list)}")

                # Run search for each SearchParams (max 5 per iteration)
                params_to_run = search_params_list[:5]
                if params_to_run:
                    commands_display: List[str] = []
                    new_files: List[ResearchFile] = []
                    for sp_dict in params_to_run:
                        if not sp_dict or not (sp_dict.get("pattern") or "").strip():
                            continue
                        try:
                            params = _dict_to_search_params(sp_dict, default_repo)
                            commands_display.append(f"{params.pattern} (repo={params.repo})")
                            files = self.search_engine.search(params)
                            print("Files found: ", len(files))
                            new_files.extend(files)
                        except Exception as e:
                            log.warning(f"[DeepResearcher] search failed for {sp_dict.get('pattern', '')!r}: {e}")
                    if commands_display:
                        _emit({"type": "commands_ran", "commands": commands_display})

                    # Deduplicate by path and merge into all_files; track newly added for history/emit
                    existing_paths = {f.path for f in all_files}
                    newly_added: List[ResearchFile] = []
                    for f in new_files:
                        if f.path not in existing_paths:
                            existing_paths.add(f.path)
                            all_files.append(f)
                            newly_added.append(f)
                    state["research_files"] = all_files[:self.max_files]
                    patterns_str = ", ".join(d.get("pattern", "") for d in params_to_run if d.get("pattern"))[:80]
                    found_str = ", ".join(f.path for f in newly_added[:5]) if newly_added else "none"
                    if len(newly_added) > 5:
                        found_str += f" (+{len(newly_added) - 5} more)"
                    search_history.append(f"{search_intent} Searched: {patterns_str}. Found: {found_str}")
                    state["search_history"] = search_history
                    try:
                        db = get_database()
                        query_json = json.dumps([{"pattern": d.get("pattern"), "repo": d.get("repo")} for d in params_to_run])
                        results_json = json.dumps([{"path": f.path, "relevance_score": f.relevance_score, "source": f.source} for f in newly_added])
                        avg_score = sum(f.relevance_score for f in newly_added) / len(newly_added) if newly_added else 0.0
                        db.save_documentation_lookup(query_json, results_json, relevance_score=avg_score)
                    except Exception as e:
                        log.warning(f"[DeepResearcher] Failed to persist retrieval: {e}")
                    history = list(state.get("retrieval_history", []))
                    for d in params_to_run:
                        p = d.get("pattern")
                        if p and p not in history:
                            history.append(p)
                    state["retrieval_history"] = history
                    if newly_added:
                        logger.logger.info(f"[DeepResearcher] Found {len(newly_added)} new files: {[f.path for f in newly_added[:3]]}...")
                    _emit({"type": "files_found", "count": len(newly_added), "paths": [f.path for f in newly_added], "files": [{"path": f.path, "source": f.source, "relevance_score": f.relevance_score, "retrieval_reason": f.retrieval_reason} for f in newly_added]})

                # Update iteration counter
                iteration += 1
                state["research_iterations"] = iteration

                # Calculate confidence from gathered files
                calculated_confidence = self._calculate_confidence(all_files, iteration)
                state["research_confidence"] = calculated_confidence

                # ANALYSE + DECIDE: after tools ran, evaluate if context is enough (reason → search → extract → analyse → decide)
                decision = "CONTINUE"
                eval_reason = ""
                if all_files:
                    decision, eval_reason = self._evaluate_context(message_text, all_files)
                    logger.logger.info(f"[DeepResearcher] Evaluate: {decision} — {eval_reason}")
                    thinking_log.append(f"Evaluate: {decision}. {eval_reason}")
                    state["thinking_log"] = thinking_log

                # Stop if evaluator says DONE, or confidence/file/iteration limits
                should_stop = (
                    decision == "DONE" or
                    state["research_confidence"] >= self.min_confidence or
                    iteration >= self.max_iterations or
                    len(all_files) >= self.max_files
                )

                if should_stop:
                    state["research_done"] = True
                    break
            
            # Build research context summary (search history + evaluation)
            state["research_context"] = {
                "total_iterations": iteration,
                "total_files": len(all_files),
                "final_confidence": state["research_confidence"],
                "thinking_summary": "\n\n".join(thinking_log),
                "search_history": search_history,
                "eval_reason": eval_reason,
            }
            # Feed downstream: research confidence (used for routing)
            state["final_confidence"] = state["research_confidence"]
            state["doc_sufficient"] = state["research_confidence"] >= Config.MIN_CONFIDENCE_TO_STOP
            
            # Log final summary
            logger.logger.info(
                f"[DeepResearcher] Completed after {iteration} iterations. "
                f"Found {len(all_files)} files. research_confidence={state['research_confidence']:.2f}"
            )
            
        except Exception as e:
            tb = traceback.format_exc()
            logger.log_error(
                error_type="DeepResearchError",
                error_message=str(e),
                stack_trace=tb,
            )
            logger.logger.exception("[DeepResearcher] Error (full traceback above)")
            print(f"[DeepResearcher] ERROR: {e}", file=sys.stderr)
            print(tb, file=sys.stderr)
            state["research_done"] = True
            state["research_confidence"] = 0.8
            state["research_error"] = True
            state["response_text"] = ""
            state["research_context"] = {"error": str(e)}
        
        return state


# Synchronous wrapper for LangGraph
def deep_researcher(state: ConversationState) -> ConversationState:
    """Synchronous wrapper for the DeepResearcher node."""
    researcher = DeepResearcher()
    return researcher(state)
