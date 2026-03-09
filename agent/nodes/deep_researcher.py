"""
Deep Researcher Node — Optimized for ripgrep + ast-grep (no vector/semantic search).

Alex, a senior OLake support engineer, plans and runs literal text (ripgrep) and
structural code (ast-grep) searches. Output is tuned for these tools: ripgrep_patterns
(identifiers, config keys, error strings) and ast_grep_patterns (func/class/type names).

Validation: After changes, run test_agent.py with a real question (e.g. snapshot
deletion), inspect retrieved files and the final answer, and iterate on prompts or
pattern execution if quality is off.
"""

from __future__ import annotations
import asyncio
import json
import re
import logging
from typing import Dict, Any, List, Optional
from pathlib import Path

from agent.state import ConversationState, ResearchFile, ReasoningIteration
from agent.llm import get_chat_completion, stream_chat_completion
from agent.logger import get_logger
from agent.config import Config, OLAKE_CONTEXT
from agent.codebase_search import CodebaseSearchEngine
from agent.github_repo_tracker import GitHubRepoTracker
from agent.persistence import get_database

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System Prompt — Optimized for ripgrep + ast-grep (no vector/semantic search)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = f"""You are Alex, a senior support engineer at OLake/Datazip.

About OLake:
{OLAKE_CONTEXT.strip()}

Your job: Research the codebase using ONLY two tools — ripgrep (text/regex in files) and ast-grep (code structure). There is NO vector search, NO semantic search, NO keyword embedding. Results come only from literal text matches and structural code patterns.

HOW THE TOOLS WORK:
1. RIPGREP — Searches for literal text or regex inside source files. A pattern matches if that exact text (or regex) appears in the file. Use for: identifiers (ExpireSnapshot, expire_snapshots), config keys (snapshot.retention), error strings, package names, comments. NOT for natural language questions or vague concepts — only strings that actually appear in code/docs.
2. AST-GREP — Searches code structure: function names, class names, method calls. The pattern is code-like: "func ExpireSnapshot" (Go), "def expire_snapshots" (Python), "class SnapshotManager". Primary language for OLake is Go. Use for finding where a function is defined or a type is used.

WHAT TO OUTPUT:
- ripgrep_patterns: List of strings that could appear literally in the codebase. Think "what would I grep for?" Examples: "expire_snapshots", "ExpireSnapshot", "snapshot", "retention", "retain", "snapshot.*expire". Use identifiers, config names, error messages — not full sentences.
- ast_grep_patterns: List of structural patterns. For Go use "func" (not "function"): "func ExpireSnapshot", "func expireSnapshots", "type SnapshotManager". Keep patterns short (one symbol per pattern).
- file_types: Prefer ["go"] for OLake; add "python", "yaml", "md" if you need config/docs.

STRATEGY:
- Start with 2-4 ripgrep patterns (identifiers and terms from the user question, converted to code-like strings). Add 1-2 ast_grep patterns to find function/type definitions.
- If the user asks "how do I X?", think: what function or config would implement X? Output those names as patterns.
- If the user asks "where is Y?", output Y and related identifiers (e.g. YManager, NewY, y_config).
- Use SEARCH HISTORY in the prompt to avoid repeating failed or irrelevant searches; plan the next search based on what was already searched and what was found.
- If a word could match many unrelated areas of the codebase, narrow by using the full phrase the user used, compound terms (e.g. "partition_regex" not just "partition"), or patterns that include the question's domain so results stay relevant.

FEW-SHOT EXAMPLES (learn the pattern: avoid single ambiguous words; use phrases/compound terms that reflect what the user is asking about):

Example 1 — User: "How can I apply partition regex to all streams at once?"
  Weak: ripgrep ["partition"] → matches Kafka partitions, drivers, tests, etc. Wrong domain.
  Strong: ripgrep ["partition_regex", "partition regex", "stream"] — use the full concept and context so matches are about stream/table config, not other features.

Example 2 — User: "Where is topic retention configured?"
  Weak: ripgrep ["retention"] → matches snapshot retention, log retention, many modules.
  Strong: ripgrep ["topic.*retention", "topic_retention", "retention"], ast_grep ["retention"] — combine with "topic" so results are about topic config.

Example 3 — User: "Postgres CDC WAL lag too high"
  Weak: ripgrep ["lag"] → matches any lag (network, replication, generic).
  Strong: ripgrep ["wal", "pgoutput", "lag", "replication"] — keep context (WAL, postgres) so results are CDC/replication-related.

OUTPUT FORMAT (valid JSON only, no markdown fences):
{{
  "thinking": "Your reasoning: what you need and why these patterns",
  "search_intent": "One or two lines: what you are searching and why. Example: Checking catalog and StreamMetadata for partition_regex so I can answer whether it applies to all streams.",
  "ripgrep_patterns": ["pattern1", "pattern2"],
  "ast_grep_patterns": ["func ExpireSnapshot", "type SnapshotManager"],
  "file_types": ["go", "yaml"],
  "problem_summary": "optional one-sentence restatement of the user question",
  "is_conceptual": false
}}

RULES:
- search_intent is REQUIRED every time: a short 1-2 lines description of what you're searching and why (e.g. "Checking X so I can Y" or "Searching for Z to find where it's configured."). This is logged and shown to you in the next iteration.
- Max 5 ripgrep_patterns and 3 ast_grep_patterns per iteration.
- Patterns must be strings that could literally appear in source (ripgrep) or structural code snippets (ast-grep). No questions, no sentences.
- If you already have many relevant files, output empty arrays to signal you are ready to evaluate (system will then ask CONTINUE/DONE).
- Set is_conceptual to true only for general-knowledge questions (e.g. "What is OLake?") that need no code search; then leave patterns empty.
"""

# Prompt for post-search evaluation: analyze retrieved data and decide if enough to answer
_EVALUATE_PROMPT = """You are evaluating whether the retrieved codebase context is enough to answer the user's question accurately.

USER QUESTION: "{message_text}"

FILES FOUND (path, why retrieved, and a snippet):
{files_summary}


Analyze the retrieved data: Does it contain the right modules, config, or docs to answer the question? If key info is missing or the files are from the wrong domain, say CONTINUE so the agent can search again with a better target.

Reply with JSON only (no markdown):
{{ "decision": "CONTINUE" | "DONE", "reason": "One sentence why." }}

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

    raise ValueError(f"Cannot parse LLM JSON even after repair: {text[:200]!r}")


# ---------------------------------------------------------------------------
# Deep Researcher Node
# ---------------------------------------------------------------------------

class DeepResearcher:
    """
    Alex - Senior OLake Support Engineer

    Thinks out loud while researching the codebase iteratively.
    Returns all gathered context with retrieval reasons.
    """

    def __init__(self):
        # Find the GitHub repos directory
        tracker = GitHubRepoTracker()
        search_dir = tracker.repos_dir or Path.cwd()
        
        self.search_engine = CodebaseSearchEngine(working_dir=search_dir)
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
    
    def _execute_searches(
        self,
        *,
        ripgrep_patterns: Optional[List[str]] = None,
        ast_grep_patterns: Optional[List[str]] = None,
        fallback_queries: Optional[List[str]] = None,
        fallback_strategy: str = "auto",
        file_types: Optional[List[str]] = None,
        thinking: str = "",
    ) -> List[ResearchFile]:
        """
        Execute searches using ripgrep and ast-grep patterns.
        Prefers ripgrep_patterns + ast_grep_patterns; falls back to fallback_queries if needed.
        """
        results: List[ResearchFile] = []
        primary_lang = (file_types or ["go"])[0]

        # New path: explicit ripgrep + ast-grep patterns
        if ripgrep_patterns or ast_grep_patterns:
            for pattern in (ripgrep_patterns or [])[:5]:
                if not (pattern and pattern.strip()):
                    continue
                query_lower = pattern.lower().strip()
                if "driver" in query_lower:
                    try:
                        find_result = self.search_engine.terminal.execute(
                            "find . -type d -name drivers",
                            working_dir=self.search_engine.working_dir,
                        )
                        if find_result.success and find_result.stdout:
                            drivers_dir = find_result.stdout.strip().split("\n")[0]
                            if drivers_dir:
                                ls_result = self.search_engine.terminal.execute(
                                    f"ls -1 {drivers_dir}",
                                    working_dir=self.search_engine.working_dir,
                                )
                                if ls_result.success and ls_result.stdout:
                                    content = f"Drivers: {drivers_dir}\n\nSubdirs:\n{ls_result.stdout}"
                                    from agent.state import ResearchFile
                                    results.append(ResearchFile(
                                        path="drivers/ (directory structure)",
                                        content=content[:5000],
                                        matches=ls_result.stdout.strip().split("\n")[:30],
                                        relevance_score=0.95,
                                        source="ripgrep",
                                        language="directory",
                                        retrieval_reason=f"Found while searching for: {pattern}",
                                    ))
                                    log.info("[DeepResearcher] Found drivers dir")
                    except Exception as e:
                        log.debug(f"Directory search failed: {e}")
                    continue
                # Ripgrep text search
                query_results = self.search_engine.search_text(
                    pattern, file_types=file_types, max_results=10,
                )
                for rf in query_results:
                    rf.retrieval_reason = f"Found while searching for: {pattern}"
                results.extend(query_results)

            # Ast-grep structural search (normalize Go: "function" -> "func")
            for pattern in (ast_grep_patterns or [])[:3]:
                if not (pattern and pattern.strip()):
                    continue
                p = pattern.strip()
                if primary_lang == "go" and p.lower().startswith("function "):
                    p = "func " + p[9:]
                try:
                    query_results = self.search_engine.search_ast(
                        p, lang=primary_lang, max_results=10,
                    )
                    for rf in query_results:
                        rf.retrieval_reason = f"Found while searching for: {pattern}"
                    results.extend(query_results)
                except Exception as e:
                    log.warning(f"ast-grep pattern '{p[:50]}' failed: {e}")

        # Fallback: legacy search_queries
        if not results and fallback_queries:
            for query in fallback_queries[:4]:
                ql = query.lower()
                if fallback_strategy == "ast-grep" or "class" in ql or "def " in ql:
                    lang = file_types[0] if file_types else "go"
                    query_results = self.search_engine.search_with_reasoning(
                        query=query, reason=thinking[:200], strategy="ast-grep",
                        file_types=[lang], max_results=10,
                    )
                else:
                    query_results = self.search_engine.search_with_reasoning(
                        query=query, reason=thinking[:200], strategy="auto",
                        file_types=file_types, max_results=10,
                    )
                results.extend(query_results)

        # Deduplicate by path
        seen = set()
        deduped = []
        for r in results:
            if r.path not in seen:
                seen.add(r.path)
                deduped.append(r)

        return deduped[:self.max_files]
    
    def _update_retrieval_reasons(
        self,
        files: List[ResearchFile],
        queries: List[str],
        thinking: str,
    ) -> None:
        """Update retrieval reasons based on what was actually found."""
        for f in files:
            if not f.retrieval_reason or f.retrieval_reason == f"Matched query:":
                # Generate a better reason based on thinking + queries
                matched_queries = [q for q in queries if q.lower() in f.content.lower() or q.lower() in f.path.lower()]
                if matched_queries:
                    f.retrieval_reason = f"Found while searching for: {', '.join(matched_queries[:2])}"
                else:
                    f.retrieval_reason = thinking[:150]
    
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
        user_id = state["user_id"]
        channel_id = state["channel_id"]
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
                # Build prompt for LLM
                user_prompt = self._build_user_prompt(state)

                _emit({"type": "thinking_start", "iteration": iteration + 1})

                # Get LLM response (streamed when callback present; chunks emitted as thinking_chunk)
                async def _streamed_planning():
                    chunks = []
                    async for delta in stream_chat_completion(
                        messages=[
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        temperature=0.3,
                    ):
                        chunks.append(delta)
                        _emit({"type": "thinking_chunk", "iteration": iteration + 1, "delta": delta})
                    return "".join(chunks)

                response = asyncio.run(_streamed_planning())

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

                ripgrep_patterns = result.get("ripgrep_patterns", []) or []
                ast_grep_patterns = result.get("ast_grep_patterns", []) or []
                if isinstance(ripgrep_patterns, str):
                    ripgrep_patterns = [ripgrep_patterns]
                if isinstance(ast_grep_patterns, str):
                    ast_grep_patterns = [ast_grep_patterns]
                fallback_queries = result.get("search_queries", []) or []
                if isinstance(fallback_queries, str):
                    fallback_queries = [fallback_queries]
                fallback_strategy = result.get("search_strategy", "auto")
                file_types = result.get("file_types")

                # Conceptual: skip code search
                if state.get("is_conceptual", False) and not all_files:
                    logger.logger.info("[DeepResearcher] Conceptual question; skipping code search")
                    state["research_confidence"] = 0.6
                    state["research_done"] = True
                    state["final_confidence"] = 0.6
                    state["doc_sufficient"] = True
                    break

                # Force at least one search when we have no files yet (derive from message_text)
                if not ripgrep_patterns and not ast_grep_patterns and not fallback_queries and len(all_files) == 0:
                    words = [w for w in message_text.strip().split() if len(w) > 2][:5]
                    ripgrep_patterns = words or ["config", "documentation"]
                    log.info(f"[DeepResearcher] No patterns from LLM; forcing ripgrep from message: {ripgrep_patterns}")

                # Log thinking and search intent (what we're searching and why — visible in logs and next iteration)
                thinking_entry = f"Iteration {iteration + 1}: {thinking}"
                thinking_log.append(thinking_entry)
                state["thinking_log"] = thinking_log

                logger.logger.info(f"[DeepResearcher] {search_intent}")
                logger.logger.info(f"[DeepResearcher] ripgrep={ripgrep_patterns}, ast_grep={ast_grep_patterns}, fallback={fallback_queries}")

                # CLI progress: commands ran (summary from patterns)
                commands_display: List[str] = []
                if ripgrep_patterns:
                    commands_display.append("ripgrep: " + ", ".join(ripgrep_patterns[:5]))
                if ast_grep_patterns:
                    commands_display.append("ast-grep: " + ", ".join(ast_grep_patterns[:3]))
                if fallback_queries and not (ripgrep_patterns or ast_grep_patterns):
                    commands_display.append("fallback: " + ", ".join(fallback_queries[:4]))
                if commands_display:
                    _emit({"type": "commands_ran", "commands": commands_display})

                has_patterns = ripgrep_patterns or ast_grep_patterns or fallback_queries
                if has_patterns:
                    new_files = self._execute_searches(
                        ripgrep_patterns=ripgrep_patterns or None,
                        ast_grep_patterns=ast_grep_patterns or None,
                        fallback_queries=fallback_queries if not (ripgrep_patterns or ast_grep_patterns) else None,
                        fallback_strategy=fallback_strategy,
                        file_types=file_types,
                        thinking=thinking,
                    )
                    all_patterns = list(ripgrep_patterns) + list(ast_grep_patterns) + list(fallback_queries)
                    self._update_retrieval_reasons(new_files, all_patterns, thinking)

                    # Accumulate files (deduplicate)
                    existing_paths = {f.path for f in all_files}
                    for f in new_files:
                        if f.path not in existing_paths:
                            all_files.append(f)
                            existing_paths.add(f.path)

                    state["research_files"] = all_files[:self.max_files]

                    # Append to search history so the next iteration knows what was searched and what was found
                    patterns_str = ", ".join(all_patterns[:6]) if all_patterns else "(none)"
                    found_str = ", ".join(f.path for f in new_files[:5]) if new_files else "none"
                    if len(new_files) > 5:
                        found_str += f" (+{len(new_files) - 5} more)"
                    search_history.append(f"{search_intent} Searched: {patterns_str}. Found: {found_str}")
                    state["search_history"] = search_history

                    # Persist retrieval batch
                    try:
                        db = get_database()
                        query_json = json.dumps({"ripgrep": ripgrep_patterns, "ast_grep": ast_grep_patterns})
                        results_json = json.dumps([
                            {"path": f.path, "relevance_score": f.relevance_score, "source": f.source}
                            for f in new_files
                        ])
                        avg_score = sum(f.relevance_score for f in new_files) / len(new_files) if new_files else 0.0
                        db.save_documentation_lookup(query_json, results_json, relevance_score=avg_score)
                    except Exception as e:
                        log.warning(f"[DeepResearcher] Failed to persist retrieval: {e}")

                    # retrieval_history for conversation persistence
                    history = list(state.get("retrieval_history", []))
                    for p in all_patterns:
                        if p and p not in history:
                            history.append(p)
                    state["retrieval_history"] = history

                    if new_files:
                        logger.logger.info(
                            f"[DeepResearcher] Found {len(new_files)} new files: "
                            f"{[f.path for f in new_files[:3]]}..."
                        )
                    _emit({
                        "type": "files_found",
                        "count": len(new_files),
                        "paths": [f.path for f in new_files],
                        "files": [{"path": f.path, "source": f.source, "relevance_score": f.relevance_score, "retrieval_reason": f.retrieval_reason} for f in new_files],
                    })

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
            # Feed solution_provider: use research confidence (no vectordb)
            state["final_confidence"] = state["research_confidence"]
            state["doc_sufficient"] = state["research_confidence"] >= Config.MIN_CONFIDENCE_TO_STOP
            
            # Log final summary
            logger.logger.info(
                f"[DeepResearcher] Completed after {iteration} iterations. "
                f"Found {len(all_files)} files. Confidence: {state['research_confidence']:.2f}"
            )
            
        except Exception as e:
            logger.log_error(
                error_type="DeepResearchError",
                error_message=str(e),
                user_id=user_id,
                channel_id=channel_id,
            )
            state["research_done"] = True
            state["research_context"] = {"error": str(e)}
        
        return state


# Synchronous wrapper for LangGraph
def deep_researcher(state: ConversationState) -> ConversationState:
    """Synchronous wrapper for the DeepResearcher node."""
    researcher = DeepResearcher()
    return researcher(state)
