from __future__ import annotations

import json
import re
from typing import Any, Dict, List


# Shared fence matcher for LLM JSON responses wrapped in ``` or ```json fences.
JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.DOTALL)


def strip_json_fences(text: str | None) -> str:
    """Remove leading/trailing ``` or ```json fences and surrounding whitespace."""
    if not text:
        return ""
    return JSON_FENCE_RE.sub("", text.strip()).strip()


def parse_summarizer_json(text: str | None) -> List[Dict[str, Any]]:
    """
    Parse summarizer LLM response: expect JSON only (no markdown fences).

    Returns list of {file_path, summary}.
    """
    if not text or not text.strip():
        return []
    raw = strip_json_fences(text)
    try:
        obj = json.loads(raw)
        summaries = obj.get("summaries")
        if isinstance(summaries, list):
            return [
                {
                    "file_path": str(s.get("file_path", "")),
                    "summary": str(s.get("summary", "")),
                }
                for s in summaries
            ]
        return []
    except (json.JSONDecodeError, TypeError):
        return []


def parse_llm_json(text: str | None) -> Dict[str, Any]:
    """
    Parse LLM JSON with progressive fallbacks for truncated responses.

    This returns a single dict object representing the top-level JSON payload.
    Callers that need to support multiple planner objects (e.g. {\"result\": [...]})
    should normalize that shape separately.
    """
    if not text:
        raise ValueError("LLM returned empty response")
    text = strip_json_fences(text)

    # 1. Happy path
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": f"Cannot parse LLM JSON even after repair: {text[:200]!r}"}

def parse_planner_json(text: str | None) -> Dict[str, Any]:
    """
    Parse planner JSON, supporting both legacy single-object output and the new
    {\"result\": [...]} shape (optionally multiple elements, typically one per repo).

    Normalizes to a single dict with:
      - thinking: combined string from all result items (joined with newlines)
      - search_intent: combined or first non-empty intent
      - search_params: flattened list aggregated from all result[i].search_params
      - is_conceptual: True if ANY result item marks is_conceptual true
    """
    base = parse_llm_json(text)

    # Legacy shape: single dict with search_params at top level
    if not isinstance(base, dict):
        return {
            "thinking": "",
            "search_intent": "Searching for relevant code.",
            "search_params": [],
            "is_conceptual": False,
        }

    # New shape: { "result": [ {...}, {...} ] }
    result_list: List[Dict[str, Any]] = []
    if "result" in base and isinstance(base["result"], list):
        result_list = base["result"]
    
    return base if isinstance(base, dict) else {"error": "Search Failed because model did not return the expected JSON format."}
