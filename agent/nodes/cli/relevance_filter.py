"""
Relevance filter for CLI mode.

AI-based filtering of codebase search results before solution generation.
"""

from __future__ import annotations
import asyncio
import json
import re
from typing import Dict, Any, List

from agent.state import ConversationState, ResearchFile
from agent.logger import get_logger
from agent.llm import get_chat_completion

_FILTER_SYSTEM = """You are a relevance filter for code search results.

Your job: Given a user question and a list of retrieved files, determine which files are actually relevant.

Rules:
- Be strict — remove files that don't directly help answer the question
- For "what is X?" questions: keep README, docs, overview files. Remove test files, utility files, unrelated implementation files
- For "how to" questions: keep examples, guides, configuration files
- For code questions: keep relevant source files, remove unrelated modules
- Test files (*_test.go, test_*.py, etc.) are rarely relevant unless the question is about testing
- Utility files (util.go, helper.py) are often irrelevant unless specifically about that utility
- Internal implementation files are often irrelevant for high-level questions

Return a JSON object with:
{{
  "relevant_paths": ["path/to/relevant1.md", "path/to/relevant2.py"],
  "reason": "Brief explanation of filtering decision"
}}

Return ONLY the JSON object, no markdown wrapper."""


async def _filter_files(
    user_query: str,
    files: List[ResearchFile],
) -> List[ResearchFile]:
    """Filter files based on relevance to the question."""
    if not files:
        return []

    # Build file descriptions for LLM
    file_descriptions = []
    for i, f in enumerate(files):
        content_preview = f.content[:500] if f.content else ""
        # Clean up content for prompt
        content_clean = content_preview.replace("\n\n", "\n").replace("  ", " ")
        file_descriptions.append(
            f"{i}. {f.path}\n"
            f"   Language: {f.language}\n"
            f"   Source: {f.source}\n"
            f"   Preview: {content_clean[:300]}"
        )

    files_text = "\n\n".join(file_descriptions)

    prompt = f"""User question: "{user_query}"

Retrieved files:
{files_text}

Return JSON with relevant file paths and filtering reason."""

    response = await get_chat_completion(
        messages=[
            {"role": "system", "content": _FILTER_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )

    # Parse response
    try:
        # Try to extract JSON from response
        response_text = response or "{}"
        # Remove markdown code fences if present
        if "```" in response_text:
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", response_text, re.DOTALL)
            if match:
                response_text = match.group(1)

        result = json.loads(response_text)
        relevant_paths = set(result.get("relevant_paths", []))

        logger = get_logger()
        logger.logger.info(f"[RelevanceFilter] Keeping {len(relevant_paths)}/{len(files)} files")

        # Filter files
        filtered = [f for f in files if f.path in relevant_paths]

        # If filtering removed everything, keep top 2 by relevance score
        if not filtered and files:
            filtered = sorted(files, key=lambda x: len(x.matches), reverse=True)[:2]

        return filtered
    except Exception as e:
        logger = get_logger()
        logger.logger.warning(f"[RelevanceFilter] Parse error: {e}, keeping all files")
        # On error, return top files by relevance score
        return sorted(files, key=lambda x: len(x.matches), reverse=True)[:8]


def cli_relevance_filter(state: ConversationState) -> ConversationState:
    """
    Filter out irrelevant files before solution generation.
    """
    logger = get_logger()

    user_id = state["user_id"]
    user_query = state["user_query"]
    research_files = state.get("research_files", [])

    if not research_files:
        return state

    try:
        # Filter files
        filtered_files = asyncio.run(_filter_files(user_query, research_files))

        # Update state with filtered files
        state["research_files"] = filtered_files

        logger.logger.info(
            f"[CLIRlevanceFilter] Filtered from {len(research_files)} to {len(filtered_files)} files"
        )

    except Exception as e:
        logger.log_error(
            error_type="CLIRlevanceFilterError",
            error_message=str(e),
            user_id=user_id,
            channel_id="cli_channel",
        )
        # On error, keep original files
        pass

    return state
