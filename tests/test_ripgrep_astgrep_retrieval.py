"""
Manual integration tests for ripgrep and ast-grep retrieval.

Run from repo root:
  uv run pytest tests/test_ripgrep_astgrep_retrieval.py -v -s

These tests require ripgrep (rg) and ast-grep to be installed.
They run real commands against the slack-agent codebase.
"""

import pytest
from pathlib import Path

# Project root (slack-agent)
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. Ripgrep CLI output format
# ---------------------------------------------------------------------------

def test_ripgrep_json_output_format():
    """Verify ripgrep --json emits parseable lines with type 'match' and expected keys."""
    from agent.terminal_tool import TerminalTool

    tool = TerminalTool()
    cmd = 'rg --json --line-number --context 0 "CodebaseSearchEngine" agent/'
    result = tool.execute(cmd, working_dir=REPO_ROOT)

    assert result.success, f"ripgrep failed: {result.error_message}"
    assert result.stdout

    import json
    match_count = 0
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        data = json.loads(line)
        if data.get("type") == "match":
            match_count += 1
            d = data["data"]
            path = d.get("path", {}).get("text", "")
            line_number = d.get("line_number", 0)
            text = d.get("lines", {}).get("text", "")
            assert path, "match should have path.text"
            assert line_number >= 0, "match should have line_number"
            assert "CodebaseSearchEngine" in (text or ""), "match line should contain pattern"
    assert match_count >= 1, "ripgrep should find at least one match for CodebaseSearchEngine"


def test_ripgrep_engine_retrieval():
    """CodebaseSearchEngine.search_text parses rg JSON and returns ResearchFiles."""
    from unittest.mock import patch
    from agent.codebase_search import CodebaseSearchEngine
    from agent.terminal_tool import CommandResult

    # Simulate ripgrep --json output (one match) so we test parsing without running rg
    mock_stdout = (
        '{"type":"match","data":{"path":{"text":"agent/codebase_search.py"},'
        '"line_number":59,"lines":{"text":"class CodebaseSearchEngine:\\n"},"submatches":[]}}\n'
    )
    mock_result = CommandResult(
        success=True, stdout=mock_stdout, stderr="", return_code=0, command="rg ..."
    )

    engine = CodebaseSearchEngine(working_dir=REPO_ROOT)
    with patch.object(engine.terminal, "execute", return_value=mock_result):
        files = engine.search_text("CodebaseSearchEngine", file_types=["python"], max_results=10)

    assert len(files) >= 1, "search_text should parse rg JSON and return ResearchFiles"
    assert files[0].source == "ripgrep"
    assert "codebase_search" in files[0].path
    assert files[0].retrieval_reason


# ---------------------------------------------------------------------------
# 2. Ast-grep CLI output format
# ---------------------------------------------------------------------------

def test_astgrep_json_output_format():
    """Verify ast-grep --json=compact returns array of objects with file, text, range."""
    from agent.terminal_tool import TerminalTool

    tool = TerminalTool()
    tool.config.max_output_length = 500_000  # ast-grep JSON can be large
    # Use a pattern that matches Python classes
    cmd = "ast-grep --json=compact --lang python --pattern 'class $$CLASS' agent/codebase_search.py"
    result = tool.execute(cmd, working_dir=REPO_ROOT)

    assert result.success, f"ast-grep failed: {result.error_message}"
    assert result.stdout

    import json
    data = json.loads(result.stdout)
    assert isinstance(data, list), "ast-grep JSON should be an array"
    assert len(data) >= 1, "should find at least one class in codebase_search.py"
    item = data[0]
    assert "file" in item or "path" in item, "each match should have file/path"
    assert "text" in item, "each match should have text"
    assert "range" in item, "each match should have range"
    line = item["range"].get("start", {}).get("line", -1)
    assert line >= 0, "range.start.line should be 0-based"
    assert "class " in (item.get("text", "") or ""), "match text should contain class"


def test_astgrep_engine_retrieval():
    """CodebaseSearchEngine.search_ast should return ResearchFiles from AST search."""
    from agent.codebase_search import CodebaseSearchEngine

    engine = CodebaseSearchEngine(working_dir=REPO_ROOT)
    # Find class definitions in Python
    files = engine.search_ast("class $$CLASS", lang="python", max_results=15)

    assert len(files) >= 1, "search_ast should find at least one file"
    for f in files:
        assert f.source == "ast-grep"
        assert f.language == "python"
        assert f.retrieval_reason
    paths = [f.path for f in files]
    assert len(paths) >= 1, "should find at least one file with a class"
    assert all(f.source == "ast-grep" and f.language == "python" for f in files)


# ---------------------------------------------------------------------------
# 3. search_with_reasoning (strategy selection)
# ---------------------------------------------------------------------------

def test_search_with_reasoning_ripgrep():
    """search_with_reasoning with strategy=ripgrep returns ripgrep results."""
    from agent.codebase_search import CodebaseSearchEngine

    engine = CodebaseSearchEngine(working_dir=REPO_ROOT)
    files = engine.search_with_reasoning(
        "ResearchFile",
        reason="Find ResearchFile type usage",
        strategy="ripgrep",
        file_types=["python"],
        max_results=10,
    )
    assert all(f.source == "ripgrep" for f in files), "strategy ripgrep should set source"
    if files:
        assert all("ResearchFile" in f.retrieval_reason or "ResearchFile" in (f.content or "") or any("ResearchFile" in m for m in (f.matches or [])) for f in files), \
            "results should relate to ResearchFile"


def test_search_with_reasoning_ast_grep():
    """search_with_reasoning with strategy=ast-grep returns ast-grep results."""
    from agent.codebase_search import CodebaseSearchEngine

    engine = CodebaseSearchEngine(working_dir=REPO_ROOT)
    files = engine.search_with_reasoning(
        "def $$FUNC",
        reason="Find function definitions",
        strategy="ast-grep",
        file_types=["python"],
        max_results=10,
    )
    assert all(f.source == "ast-grep" for f in files), "strategy ast-grep should set source"


def test_smart_search_fallback():
    """smart_search (strategy=auto) should return results for a concept query."""
    from agent.codebase_search import CodebaseSearchEngine

    engine = CodebaseSearchEngine(working_dir=REPO_ROOT)
    files = engine.smart_search("codebase search", context={"file_types": ["python"]}, max_results=10)
    # May be empty on minimal trees; at least no crash and structure is correct
    for f in files:
        assert hasattr(f, "path") and hasattr(f, "source")
        assert f.source in ("ripgrep", "ast-grep")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
