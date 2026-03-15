"""
State management for OLake Slack Community Agent.

Defines state schema for conversation handling with LangGraph.
"""

from typing import TypedDict, List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class IntentType(Enum):
    """Types of user intents."""
    QUESTION = "question"
    ISSUE = "issue"
    DISCUSSION = "discussion"
    FEEDBACK = "feedback"
    UNKNOWN = "unknown"


class UrgencyLevel(Enum):
    """Urgency levels for issues."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class UserProfile:
    """User profile with interaction history."""
    user_id: str
    username: str
    real_name: str
    email: Optional[str]
    total_messages: int
    common_topics: List[str]
    resolved_issues: int
    unresolved_issues: int
    avg_resolution_time: float  # in minutes
    last_interaction: Optional[datetime]
    knowledge_level: str  # "beginner", "intermediate", "advanced"
    

@dataclass
class ResearchFile:
    """A file retrieved during codebase research."""
    path: str
    content: str
    matches: List[str]  # matching lines/snippets
    relevance_score: float
    source: str  # "ripgrep" | "ast-grep"
    language: str
    retrieval_reason: str  # Why this file was retrieved (small summary)
    search_pattern: Optional[str] = None  # Pattern used for retrieval (e.g. ripgrep/ast-grep pattern)


@dataclass
class ReasoningIteration:
    """A single iteration of the reasoning process."""
    iteration: int
    thought_process: str
    confidence: float
    decision: str            # "ANSWER" | "CLARIFY" | "RETRIEVE_MORE"
    needs_more_docs: bool
    needs_clarification: bool
    identified_gaps: List[str]
    new_search_queries: List[str]


class ConversationState(TypedDict):
    """
    State for the Slack community agent conversation graph.

    This state is passed between nodes in the LangGraph workflow.

    Note: CLI may set an optional key _cli_progress_callback (callable) for
    live progress reporting; it is not part of the TypedDict and is not persisted.
    """
    # Slack event data
    event: Dict[str, Any]  # Raw Slack event
    channel_id: str
    user_id: str
    message_text: str
    thread_ts: Optional[str]  # Thread timestamp for replies
    message_ts: str  # Message timestamp
    
    # User context
    user_profile: Optional[UserProfile]
    previous_messages: List[Dict[str, Any]]  # User's message history
    thread_context: List[Dict[str, Any]]  # Messages in current thread
    
    # Intent analysis
    intent_type: Optional[IntentType]
    urgency: Optional[UrgencyLevel]
    key_topics: List[str]
    technical_terms: List[str]
    
    # Optional summary/signals (set by deep_researcher when present in LLM output)
    problem_summary: Optional[str]        # one-sentence restatement of the issue
    sub_questions: List[str]              # (unused; kept for compatibility)
    is_ambiguous: bool                    # (unused; kept for compatibility)
    is_conceptual: bool                   # True if question can be answered without code search

    # OLake context (summarised for this turn by olake_context_summariser when used)
    needs_codebase_search: Optional[bool]  # True if question requires codebase search; False = generic, go straight to solution_provider
    about_olake_summary: Optional[str]   # focused excerpt of ABOUT_OLAKE for this query; set only when needs_codebase_search; else empty
    relevant_repos: Optional[List[str]]  # repo names to prefer for this question; set only when needs_codebase_search; else empty
    relevant_repos_detail: Optional[List[Dict[str, Any]]]  # per-repo summary_points and connections; set only when needs_codebase_search

    # Deep Research Agent
    research_context: Dict[str, Any]      # accumulated research data
    research_iterations: int              # how many research iterations completed
    max_research_iterations: int          # cap for research iterations
    thinking_log: List[str]               # Alex's thoughts per iteration
    search_history: List[str]             # what was searched, why, and what was found (for next iteration)
    research_files: List[ResearchFile]    # files found during research
    research_files_summary: Optional[str] # LLM-generated summary of research_files (for planner on next iteration)
    eval_reason: Optional[str]            # last evaluator reason (CONTINUE/DONE) so planner knows why we continue
    research_done: bool                   # True when research is complete
    research_confidence: float            # confidence in gathered context (0-1); used for routing

    # Reasoning process
    reasoning_iterations: List[ReasoningIteration]
    current_iteration: int
    final_confidence: float
    solution_found: bool
    reasoning_trace: Optional[str]        # deep_reasoner chain-of-thought (logged)
    reasoner_decision: Optional[str]      # "ANSWER" | "CLARIFY" | "RETRIEVE_MORE"
    
    # Response generation
    needs_clarification: bool
    clarification_questions: List[str]
    should_escalate: bool
    escalation_reason: Optional[str]
    response_text: Optional[str]
    response_blocks: Optional[List[Dict[str, Any]]]

    # Org-member guard
    org_member_replied: bool  # True when an org team member is in the thread → bot silences
    doc_sufficient: bool       # True when retrieved docs score above DOCS_ANSWER_THRESHOLD
    # Metadata
    processing_start_time: datetime
    processing_end_time: Optional[datetime]
    total_processing_time: Optional[float]  # in seconds
    error: Optional[str]


@dataclass
class ConversationRecord:
    """Record of a conversation for database storage."""
    id: Optional[int]
    message_ts: str
    thread_ts: Optional[str]
    channel_id: str
    user_id: str
    message_text: str
    intent_type: str
    urgency: str
    response_text: Optional[str]
    confidence: float
    needs_clarification: bool
    escalated: bool
    escalation_reason: Optional[str]
    docs_cited: Optional[str]  # JSON string
    reasoning_summary: Optional[str]
    processing_time: float
    created_at: datetime
    resolved: bool
    resolved_at: Optional[datetime]
    retrieval_queries: Optional[str] = None   # JSON array of queries run
    retrieval_file_paths: Optional[str] = None  # JSON array of file paths retrieved


@dataclass
class UserInteraction:
    """Record of a user interaction for profiling."""
    id: Optional[int]
    user_id: str
    message_ts: str
    channel_id: str
    topic: str
    resolved: bool
    resolution_time: Optional[float]  # in minutes
    created_at: datetime


def create_initial_state(event: Dict[str, Any]) -> ConversationState:
    """
    Create initial conversation state from Slack event.
    
    Args:
        event: Slack event dict
        
    Returns:
        Initial ConversationState
    """
    message_event = event.get("event", {})
    
    return ConversationState(
        # Event data
        event=event,
        channel_id=message_event.get("channel", ""),
        user_id=message_event.get("user", ""),
        message_text=message_event.get("text", ""),
        thread_ts=message_event.get("thread_ts"),
        message_ts=message_event.get("ts", ""),
        
        # User context
        user_profile=None,
        previous_messages=[],
        thread_context=[],
        
        # Intent analysis
        intent_type=None,
        urgency=None,
        key_topics=[],
        technical_terms=[],
        
        # Optional summary/signals (deep_researcher may set)
        problem_summary=None,
        sub_questions=[],
        is_ambiguous=False,
        is_conceptual=False,

        # Deep Research Agent
        research_context={},
        research_iterations=0,
        max_research_iterations=5,
        thinking_log=[],
        search_history=[],
        research_files=[],
        research_done=False,
        research_confidence=0.0,

        # Reasoning
        reasoning_iterations=[],
        current_iteration=0,
        final_confidence=0.0,
        solution_found=False,
        reasoning_trace=None,
        reasoner_decision=None,
        
        # Response
        needs_clarification=False,
        clarification_questions=[],
        should_escalate=False,
        escalation_reason=None,
        response_text=None,
        response_blocks=None,

        # Org-member guard
        org_member_replied=False,
        doc_sufficient=False,
        
        # Metadata
        processing_start_time=datetime.now(),
        processing_end_time=None,
        total_processing_time=None,
        error=None
    )
