"""
Codebase Search Engine using ripgrep and ast-grep.

Provides smart search capabilities for the Deep Research Agent.
"""

import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import logging

from agent.state import ResearchFile
from agent.terminal_tool import TerminalTool, TerminalToolConfig

log = logging.getLogger(__name__)


# File type mappings for ripgrep
FILE_TYPE_MAP = {
    "python": (".py",),
    "javascript": (".js", ".jsx", ".mjs"),
    "typescript": (".ts", ".tsx"),
    "go": (".go",),
    "rust": (".rs",),
    "java": (".java",),
    "yaml": (".yaml", ".yml"),
    "json": (".json",),
    "markdown": (".md",),
    "shell": (".sh", ".bash", ".zsh"),
    "sql": (".sql",),
}

# Language codes for ast-grep
AST_GREP_LANG_MAP = {
    "python": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "yaml": "yaml",
    "json": "json",
}


@dataclass
class SearchParams:
    """
    Standardized parameters for both ripgrep and ast-grep searches.
    Each tool uses the fields it needs; the rest are ignored.
    """
    pattern: str
    repo: str = "olake"
    max_results: int = 50
    file_types: Optional[list[str]] = None   # ripgrep: glob filter; ast-grep: can infer lang from first
    lang: Optional[str] = None                # ast-grep; if None and file_types set, first file_type used as lang
    exclude_dirs: Optional[list[str]] = None # ripgrep only
    context_lines: int = 2                    # ripgrep only


@dataclass
class SearchResult:
    """Raw search result before conversion to ResearchFile."""
    file_path: str
    line_number: int
    content: str
    match_text: str


class CodebaseSearchEngine:
    """
    Smart codebase search engine using ripgrep and ast-grep.
    
    Provides multiple search strategies:
    - Text search (ripgrep): Fast keyword matching
    - AST search (ast-grep): Structural code pattern matching
    - Smart search: Auto-selects strategy based on query
    """
    
    def __init__(self):
        """
        Initialize the search engine.
        """
        self.working_dir = Path.cwd() / ".github_repos"
        self.terminal = TerminalTool()
        # Allow large output so rg/ast-grep JSON is not truncated (would break parsing)
        self.terminal.config.max_output_length = 500_000
        
    def _detect_language(self, file_path: str) -> str:
        """Detect programming language from file extension."""
        ext = Path(file_path).suffix.lower()
        for lang, extensions in FILE_TYPE_MAP.items():
            if ext in extensions:
                return lang
        return "unknown"
    
    def _search_dir(self, repo: str = "olake") -> Path:
        """Resolve directory to run search in: working_dir or working_dir/repo."""
        return self.working_dir / repo if repo else self.working_dir

    def _run_ripgrep(self, params: SearchParams) -> list[SearchResult]:
        """Run ripgrep search. Uses pattern, repo, max_results, file_types, exclude_dirs, context_lines."""
        print(params)
        search_dir = self._search_dir(params.repo)
        # Build ripgrep command
        cmd_parts = ["rg", "--json", "--line-number", "--context", str(params.context_lines)]
        
        # Add file type filters
        if params.file_types:
            for ft in params.file_types:
                if ft in FILE_TYPE_MAP:
                    for ext in FILE_TYPE_MAP[ft]:
                        cmd_parts.extend(["--glob", f"*{ext}"])
        
        # Add directory exclusions
        exclude = params.exclude_dirs or ["__pycache__", ".git", "node_modules", "venv", ".venv"]
        for exc in exclude:
            cmd_parts.extend(["--glob", f"!**/{exc}/**"])
        
        # Add pattern
        cmd_parts.extend(["--", params.pattern])
        
        # Quote so shell does not expand ! (history) or * (glob)
        cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        
        try:
            if not search_dir.exists() or not search_dir.is_dir():
                log.warning(f"[ripgrep] Search dir does not exist or is not a directory: {search_dir}")
                return []
            result = self.terminal.execute(cmd, working_dir=search_dir)
            
            # Log the command being executed
            log.info(f"[ripgrep] Executing: {cmd}")
            log.info(f"[ripgrep] Working dir: {search_dir}")
            log.info(f"[ripgrep] Found {len(result.stdout) if result.stdout else 0} bytes of output")

            if not result.success and result.error_message and "not found" in result.error_message:
                log.warning(f"ripgrep not available: {result.error_message}")
                return []

            if not result.stdout:
                return []

            results = []
            for line in (result.stdout or "").strip().split("\n"):
                if not line.strip():
                    continue
                try:
                    import json
                    data = json.loads(line)
                    
                    # Parse ripgrep JSON output
                    if "type" in data:
                        if data["type"] == "match":
                            match_data = data["data"]
                            file_path = match_data.get("path", {}).get("text", "")
                            line_num = match_data.get("line_number", 0)
                            match_text = match_data.get("lines", {}).get("text", "")
                            
                            # Get context from surrounding lines if available
                            content = match_text
                            
                            results.append(SearchResult(
                                file_path=file_path,
                                line_number=line_num,
                                content=content,
                                match_text=match_text,
                            ))
                        elif data["type"] == "context":
                            # Context lines - append to previous result if same file
                            match_data = data["data"]
                            if results and results[-1].file_path == match_data.get("path", {}).get("text", ""):
                                ctx_text = match_data.get("lines", {}).get("text", "")
                                results[-1].content += "\n" + ctx_text
                                
                except (json.JSONDecodeError, KeyError) as e:
                    log.debug(f"Failed to parse ripgrep output line: {e}")
                    continue
            
            return results[:params.max_results]
            
        except Exception as e:
            log.error(f"ripgrep search failed: {e}")
            return []
    
    def _run_ast_grep(self, params: SearchParams) -> list[SearchResult]:
        """Run ast-grep structural search. Uses pattern, repo, max_results, lang (or first of file_types)."""
        lang = params.lang or (params.file_types[0] if params.file_types else None) or "python"
        if lang not in AST_GREP_LANG_MAP:
            log.warning(f"ast-grep doesn't support language: {lang}")
            return []
        search_dir = self._search_dir(params.repo)
        if not search_dir.exists() or not search_dir.is_dir():
            log.warning(f"[ast-grep] Search dir does not exist or is not a directory: {search_dir}")
            return []
        
        cmd = f"ast-grep --json=compact --lang {AST_GREP_LANG_MAP[lang]} --pattern '{params.pattern}'"
        
        try:
            result = self.terminal.execute(cmd, working_dir=search_dir)
            
            # Log the command being executed
            log.info(f"[ast-grep] Executing: {cmd}")
            log.info(f"[ast-grep] Working dir: {search_dir}")

            if not result.success and result.error_message and "not found" in result.error_message:
                log.warning(f"ast-grep not available: {result.error_message}")
                return []

            if not result.stdout:
                return []
            
            results = []
            import json
            
            # ast-grep outputs JSON array: each item has file, text, range (0-based line)
            try:
                data = json.loads(result.stdout)
                if isinstance(data, list):
                    for item in data[:params.max_results]:
                        file_path = item.get("file", item.get("path", ""))
                        r = item.get("range", {})
                        line_num = r.get("start", {}).get("line", 0)
                        match_text = item.get("text", item.get("match", {}).get("text", ""))
                        
                        # Get surrounding content
                        content = match_text
                        
                        results.append(SearchResult(
                            file_path=file_path,
                            line_number=line_num + 1,  # Convert to 1-based
                            content=content,
                            match_text=match_text,
                        ))
            except json.JSONDecodeError as e:
                log.error(f"Failed to parse ast-grep output: {e}")
                
            return results
            
        except Exception as e:
            log.error(f"ast-grep search failed: {e}")
            return []
    
    def search_text(
        self,
        query: str,
        file_types: Optional[list[str]] = None,
        max_results: int = 30,
        repo: Optional[str] = None,
    ) -> list[ResearchFile]:
        """
        Search using ripgrep (text-based).
        
        Args:
            query: Search query (supports regex)
            file_types: Optional file type filters
            max_results: Maximum results
            repo: If set, search only in this repo (subdir of working_dir, e.g. "olake").
            
        Returns:
            List of ResearchFile objects (path prefixed with repo/ when repo is set).
        """
        params = SearchParams(
            pattern=query,
            repo=repo,
            max_results=max_results,
            file_types=file_types,
        )
        return self.search(params)
    
    def search_ast(
        self,
        pattern: str,
        lang: str = "python",
        max_results: int = 30,
        repo: Optional[str] = None,
    ) -> list[ResearchFile]:
        """
        Search using ast-grep (structural).
        
        Args:
            pattern: AST pattern
            lang: Programming language
            max_results: Maximum results
            repo: If set, search only in this repo (subdir of working_dir).
            
        Returns:
            List of ResearchFile objects (path prefixed with repo/ when repo is set).
        """
        params = SearchParams(
            pattern=pattern,
            repo=repo,
            max_results=max_results,
            lang=lang,
        )
        return self.search(params)
    
    def search(self, params: SearchParams) -> list[ResearchFile]:
        """
        Run both ripgrep and ast-grep with the same params, merge results,
        deduplicate by (file_path, line_number), and return ResearchFiles.
        """
        raw_rg = self._run_ripgrep(params)
        raw_ast = self._run_ast_grep(params)
        combined = raw_rg + raw_ast
        # Deduplicate by (file_path, line_number); keep first occurrence
        seen: set[tuple[str, int]] = set()
        unique: list[SearchResult] = []
        for r in combined:
            path = r.file_path.lstrip("./")
            key = (path, r.line_number)
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)
        return self._convert_to_research_files(
            unique, params.pattern, repo=params.repo
        )
    
    def _convert_to_research_files(
        self,
        results: list[SearchResult],
        query: str,
        repo: Optional[str] = None,
    ) -> list[ResearchFile]:
        """Convert raw search results to ResearchFile objects with deduplication."""
        base_path = self._search_dir(repo)
        # Group by file path (keep path relative to search dir; only strip leading "./" if present)
        file_groups: dict[str, list[SearchResult]] = {}
        for r in results:
            file_path = r.file_path
            if file_path.startswith("./"):
                file_path = file_path[2:]
            if file_path not in file_groups:
                file_groups[file_path] = []
            file_groups[file_path].append(r)
        
        research_files = []
        for file_path, matches in file_groups.items():
            # Read full file content from the directory we searched
            full_path = base_path / file_path
            try:
                content = full_path.read_text()[:10000]  # Limit content size
            except Exception:
                content = "\n".join(m.content for m in matches)
            
            # Prefix path with repo name so callers see e.g. "olake/pkg/foo.go"
            display_path = f"{repo}/{file_path}" if repo else file_path
            
            research_files.append(ResearchFile(
                path=display_path,
                content=content,
                matches=[m.match_text for m in matches[:10]],  # Top 10 matches
                retrieval_reason=f"Matched query: {query}",
                search_pattern=query,
            ))
        
        return research_files
    
    def smart_search(
        self,
        query: str,
        context: Optional[dict] = None,
        max_results: int = 30,
        repo: Optional[str] = None,
    ) -> list[ResearchFile]:
        """
        Smart search that auto-selects the best strategy.
        
        Strategy selection:
        - Function/class names → ast-grep
        - Error messages → ripgrep (exact match)
        - Concepts/topics → ripgrep (broad search)
        - API endpoints → ripgrep (route patterns)
        
        Args:
            query: Search query
            context: Optional context (detected file types, previous results, etc.)
            max_results: Maximum results
            repo: If set, search only in this repo (subdir of working_dir).
            
        Returns:
            List of ResearchFile objects
        """
        context = context or {}
        all_results: list[ResearchFile] = []
        
        # Detect query type and select strategy
        query_lower = query.lower()
        
        # Strategy 1: Function/class definition search
        func_patterns = [
            r"def\s+\w+",
            r"class\s+\w+",
            r"function\s+\w+",
            r"interface\s+\w+",
        ]
        if any(re.match(p, query_lower) for p in func_patterns):
            # Extract name and use ast-grep
            name_match = re.search(r"\w+", query_lower.split()[-1])
            if name_match:
                name = name_match.group()
                lang = context.get("language", "python")
                ast_results = self.search_ast(f"class {name}", lang=lang, max_results=max_results, repo=repo)
                if ast_results:
                    return ast_results
        
        # Strategy 2: Error message or exact string search
        if '"' in query or "'" in query or "error" in query_lower:
            # Use ripgrep with exact match
            text_results = self.search_text(
                query,
                file_types=context.get("file_types"),
                max_results=max_results,
                repo=repo,
            )
            if text_results:
                return text_results
        
        # Strategy 3: Concept/topic search (default)
        # Build multiple search patterns for better coverage
        terms = query.split()
        
        # Search for the full query
        full_results = self.search_text(
            query,
            file_types=context.get("file_types"),
            max_results=max_results // 2,
            repo=repo,
        )
        all_results.extend(full_results)
        
        # Search for individual important terms
        if len(terms) > 1:
            for term in terms:
                if len(term) > 3:  # Skip short words
                    term_results = self.search_text(
                        term,
                        file_types=context.get("file_types"),
                        max_results=max_results // 4,
                        repo=repo,
                    )
                    all_results.extend(term_results)
        
        # Deduplicate by file path
        seen_paths = set()
        deduped = []
        for r in sorted(all_results, key=lambda x: len(x.matches), reverse=True):
            if r.path not in seen_paths:
                seen_paths.add(r.path)
                deduped.append(r)
        
        return deduped[:max_results]
    
    def search_with_reasoning(
        self,
        query: str,
        reason: str,
        strategy: str = "auto",
        file_types: Optional[list[str]] = None,
        max_results: int = 20,
        repo: Optional[str] = None,
    ) -> list[ResearchFile]:
        """
        Search with a reasoning explanation for why we're searching.
        
        Args:
            query: Search query
            reason: Why we're searching for this (for retrieval_reason)
            strategy: "ripgrep", "ast-grep", or "auto"
            file_types: Optional file type filters
            max_results: Maximum results
            repo: If set, search only in this repo (subdir of working_dir).
            
        Returns:
            List of ResearchFile objects with populated retrieval_reason
        """
        if strategy == "auto":
            results = self.smart_search(query, {"file_types": file_types}, max_results, repo=repo)
        elif strategy == "ripgrep":
            results = self.search_text(query, file_types, max_results, repo=repo)
        elif strategy == "ast-grep":
            lang = file_types[0] if file_types else "python"
            results = self.search_ast(query, lang, max_results, repo=repo)
        else:
            results = []
        
        # Keep per-query reason so each file shows which query found it (not the same
        # generic "thinking" for all files, which made "Files Retrieved" look identical)
        for rf in results:
            rf.retrieval_reason = f"Found while searching for: {query}"
        
        return results
