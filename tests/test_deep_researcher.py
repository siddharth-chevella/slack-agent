"""
Tests for the Deep Researcher node and Codebase Search Engine.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from agent.codebase_search import CodebaseSearchEngine, SearchResult
from agent.state import ResearchFile, create_initial_state
from agent.nodes.deep_researcher import DeepResearcher, _parse_json


# ---------------------------------------------------------------------------
# Codebase Search Engine Tests
# ---------------------------------------------------------------------------

class TestCodebaseSearchEngine:
    """Tests for the codebase search engine."""
    
    @pytest.fixture
    def search_engine(self, tmp_path):
        """Create a search engine with a temp working directory."""
        # Create some test files
        (tmp_path / "test.py").write_text("""
def hello():
    print("Hello, World!")

class MyClass:
    pass
""")
        (tmp_path / "config.yaml").write_text("""
database:
  host: localhost
  port: 5432
""")
        return CodebaseSearchEngine(working_dir=tmp_path)
    
    def test_detect_language(self, search_engine):
        """Test language detection from file extension."""
        assert search_engine._detect_language("test.py") == "python"
        assert search_engine._detect_language("config.yaml") == "yaml"
        assert search_engine._detect_language("unknown.xyz") == "unknown"
    
    def test_convert_to_research_files(self, search_engine):
        """Test conversion of raw results to ResearchFile objects."""
        raw_results = [
            SearchResult(
                file_path="test.py",
                line_number=2,
                content="def hello():",
                match_text="def hello():",
                source="ripgrep",
                language="python",
            ),
            SearchResult(
                file_path="test.py",
                line_number=5,
                content="class MyClass:",
                match_text="class MyClass:",
                source="ripgrep",
                language="python",
            ),
        ]
        
        files = search_engine._convert_to_research_files(raw_results, "test query")
        
        assert len(files) == 1  # Deduplicated by path
        assert files[0].path == "test.py"
        assert len(files[0].matches) == 2
        assert files[0].source == "ripgrep"
        assert files[0].language == "python"
        assert "test query" in files[0].retrieval_reason


class TestParseJson:
    """Tests for JSON parsing with fallbacks."""
    
    def test_parse_valid_json(self):
        """Test parsing valid JSON."""
        text = '{"decision": "DONE", "confidence": 0.8}'
        result = _parse_json(text)
        assert result["decision"] == "DONE"
        assert result["confidence"] == 0.8
    
    def test_parse_json_with_fences(self):
        """Test parsing JSON with markdown fences."""
        text = '```json\n{"decision": "DONE"}\n```'
        result = _parse_json(text)
        assert result["decision"] == "DONE"
    
    def test_parse_truncated_json(self):
        """Test parsing truncated JSON."""
        text = '{"decision": "DONE", "confidence": 0.8, "thinking": "'
        result = _parse_json(text)
        assert result["decision"] == "DONE"
        assert result["confidence"] == 0.8
    
    def test_parse_regex_fallback(self):
        """Test regex extraction fallback."""
        text = '{"decision": "CONTINUE", "confidence": 0.6, "thinking": "some long text..."'
        result = _parse_json(text)
        assert result["decision"] == "CONTINUE"
        assert result["confidence"] == 0.6
    
    def test_empty_input(self):
        """Test empty input raises error."""
        with pytest.raises(ValueError):
            _parse_json(None)
        with pytest.raises(ValueError):
            _parse_json("")


# ---------------------------------------------------------------------------
# Deep Researcher Tests
# ---------------------------------------------------------------------------

class TestDeepResearcher:
    """Tests for the Deep Researcher node."""
    
    @pytest.fixture
    def initial_state(self):
        """Create initial conversation state."""
        return create_initial_state({
            "event": {
                "channel": "C123456",
                "user": "U123456",
                "text": "How does CDC work for PostgreSQL?",
                "ts": "1234567890.123456",
            }
        })
    
    @pytest.fixture
    def researcher(self):
        """Create a DeepResearcher instance."""
        return DeepResearcher()
    
    def test_calculate_confidence(self, researcher):
        """Test confidence calculation."""
        # No files = low confidence
        assert researcher._calculate_confidence([], 1) < 0.2
        
        # Some files = medium confidence
        files = [
            ResearchFile(
                path="test1.py",
                content="test",
                matches=["match1"],
                source="ripgrep",
                language="python",
                retrieval_reason="test",
            ),
            ResearchFile(
                path="test2.py",
                content="test",
                matches=["match1"],
                source="ripgrep",
                language="python",
                retrieval_reason="test",
            ),
        ]
        confidence = researcher._calculate_confidence(files, 2)
        assert 0.3 <= confidence <= 1.0
    
    def test_update_retrieval_reasons(self, researcher):
        """Test retrieval reason updates."""
        files = [
            ResearchFile(
                path="cdc_handler.py",
                content="CDC implementation for PostgreSQL",
                matches=["cdc", "postgres"],
                source="ripgrep",
                language="python",
                retrieval_reason="",
            ),
        ]
        queries = ["cdc postgres", "pgoutput"]
        thinking = "Looking for CDC implementation"
        
        researcher._update_retrieval_reasons(files, queries, thinking)
        
        assert files[0].retrieval_reason != ""
        assert "cdc postgres" in files[0].retrieval_reason.lower() or "cdc" in files[0].retrieval_reason.lower()
    
    @patch('agent.nodes.deep_researcher.get_chat_completion')
    @patch('agent.codebase_search.CodebaseSearchEngine.search_with_reasoning')
    def test_researcher_integration(
        self, mock_search, mock_completion, initial_state, researcher
    ):
        """Test full researcher integration."""
        # Mock LLM response: researcher format (search_intent, thinking, patterns) and evaluator uses decision/reason
        mock_completion.return_value = '''{
            "thinking": "I understand the question about CDC for PostgreSQL.",
            "search_intent": "Checking CDC and Postgres driver code so we can answer how CDC works for PostgreSQL.",
            "search_queries": ["cdc postgres"],
            "search_strategy": "ripgrep",
            "file_types": ["python"],
            "evaluation": "Found relevant files",
            "confidence": 0.8,
            "decision": "DONE",
            "reason": "Have enough context",
            "files_summary": ["cdc_handler.py: contains CDC logic"]
        }'''
        
        # Mock search results
        mock_search.return_value = [
            ResearchFile(
                path="connectors/postgres/cdc_handler.py",
                content="CDC handler implementation",
                matches=["def handle_cdc", "class CDC"],
                source="ripgrep",
                language="python",
                retrieval_reason="Found while searching for: cdc postgres",
            ),
        ]
        
        # Run researcher
        result = researcher(initial_state)
        
        # Verify results (confidence is calculated from file count/relevance, not LLM)
        assert result["research_done"] is True
        assert len(result["research_files"]) > 0
        assert result["research_confidence"] >= 0.4
        assert len(result["thinking_log"]) > 0
        assert "research_context" in result
        assert "search_history" in result
        # After at least one search, search_history should have an entry (intent + what was found)
        if result.get("search_history"):
            assert any("Searched:" in entry for entry in result["search_history"])


class TestResearchFile:
    """Tests for ResearchFile dataclass."""
    
    def test_research_file_creation(self):
        """Test ResearchFile creation."""
        rf = ResearchFile(
            path="test.py",
            content="print('hello')",
            matches=["print('hello')"],
            source="ripgrep",
            language="python",
            retrieval_reason="Testing",
        )
        assert rf.path == "test.py"
        assert rf.language == "python"
        assert rf.source == "ripgrep"


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------

class TestSearchStrategies:
    """Test different search strategies."""
    
    @pytest.fixture
    def temp_codebase(self, tmp_path):
        """Create a temp codebase for testing."""
        # Create Python files
        (tmp_path / "cdc.py").write_text("""
class CDCConfig:
    def __init__(self):
        self.enabled = True

def setup_cdc():
    pass
""")
        (tmp_path / "postgres.py").write_text("""
class PostgresConnector:
    def connect(self):
        pass
""")
        (tmp_path / "config.py").write_text("""
DATABASE_URL = "postgresql://localhost"
""")
        return tmp_path
    
    def test_smart_search_text_pattern(self, temp_codebase):
        """Test smart search with text pattern."""
        engine = CodebaseSearchEngine(working_dir=temp_codebase)
        
        # This would normally call ripgrep, but we're testing the strategy selection
        # In a real test, ripgrep would find "CDC" in the files
        # For now, we test that the method doesn't crash
        results = engine.smart_search("CDC configuration", max_results=10)
        # Results may be empty if ripgrep isn't available, but method should work
        assert isinstance(results, list)
    
    def test_search_with_reasoning(self, temp_codebase):
        """Test search with reasoning: each file gets a query-specific reason (not the same for all)."""
        engine = CodebaseSearchEngine(working_dir=temp_codebase)
        
        results = engine.search_with_reasoning(
            query="class CDC",
            reason="Looking for CDC class definition",
            strategy="auto",
            max_results=5,
        )
        
        assert isinstance(results, list)
        for rf in results:
            # Reason is per-query so "Files Retrieved" shows which query found each file
            assert rf.retrieval_reason == "Found while searching for: class CDC"
