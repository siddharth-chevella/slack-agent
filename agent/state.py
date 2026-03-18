"""
State management for OLake Slack Community Agent.

Defines state schema for conversation handling with LangGraph.
"""

from typing import TypedDict, List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Annotated
from langgraph.reducers import add_messages


class IntentType(Enum):
    """Types of user intents."""
    QUESTION = "question"
    ISSUE = "issue"
    DISCUSSION = "discussion"
    FEEDBACK = "feedback"
    UNKNOWN = "unknown"


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
    event: Annotated[Dict[str, Any], add_messages]  # Raw Slack event
    channel_id: Annotated[str, add_messages]
    user_id: Annotated[str, add_messages]
    user_query: str
    thread_ts: Annotated[Optional[str], add_messages]  # Thread timestamp for replies
    message_ts: Annotated[str, add_messages]  # Message timestamp
    
    thread_context: Annotated[List[Dict[str, Any]], add_messages]  # Messages in current thread
    
    # Optional summary/signals (set by deep_researcher when present in LLM output)
    is_conceptual: Annotated[bool, add_messages]                   # True if question can be answered without code search

    # OLake context (summarised for this turn by olake_context_summariser when used)
    needs_codebase_search: Annotated[Optional[bool], add_messages]  # True if question requires codebase search; False = generic, go straight to solution_provider

    # Deep Research Agent
    thinking_log: Annotated[List[str], add_messages]               # Alex's thoughts per iteration
    search_history: Annotated[List[str], add_messages]             # what was searched, why, and what was found (for next iteration)
    research_files: List[ResearchFile]    # files found during research
    research_files_summary: Annotated[Optional[str], add_messages] # LLM-generated summary of research_files (for planner on next iteration)
    eval_reason: Annotated[Optional[str], add_messages]            # last evaluator reason (CONTINUE/DONE) so planner knows why we continue
    research_done: bool                   # True when research is complete
    research_confidence: Annotated[float, add_messages]            # confidence in gathered context (0-1); used for routing

    # Reasoning process
    final_confidence: Annotated[float, add_messages]
    reasoning_trace: Annotated[Optional[str], add_messages]        # deep_reasoner chain-of-thought (logged)
    
    # Response generation
    needs_clarification: bool
    clarification_questions: Annotated[List[str], add_messages]
    should_escalate: bool
    escalation_reason: Annotated[Optional[str], add_messages]
    response_text: Annotated[Optional[str], add_messages]
    response_blocks: Annotated[Optional[List[Dict[str, Any]]], add_messages]

    # Org-member guard
    org_member_replied: bool  # True when an org team member is in the thread → bot silences
    doc_sufficient: bool       # True when retrieved docs score above DOCS_ANSWER_THRESHOLD
    # Metadata
    processing_start_time: Annotated[datetime, add_messages]
    error: Annotated[Optional[str], add_messages]


@dataclass
class ConversationRecord:
    """Record of a conversation for database storage."""
    id: Optional[int]
    message_ts: str
    thread_ts: Optional[str]
    channel_id: str
    user_id: str
    user_query: str
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
        user_query=message_event.get("text", ""),
        thread_ts=message_event.get("thread_ts"),
        message_ts=message_event.get("ts", ""),
        
        # Thread context
        thread_context=[],
        
        # Optional summary/signals (deep_researcher may set)
        is_conceptual=False,

        # Deep Research Agent
        thinking_log=[],
        search_history=[],
        research_files=[],
        research_done=False,
        research_confidence=0.0,

        # Reasoning
        final_confidence=0.0,
        reasoning_trace=None,
        
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
        error=None
    )
