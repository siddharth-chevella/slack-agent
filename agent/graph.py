"""
LangGraph workflow for OLake Slack Community Agent (production).

Used by main.py in production and by test_agent.py for local testing — keep
both in sync so test runs reflect prod behavior.

Topology:
  build_context
       └─ [org member in thread] → END silently
  → olake_context_summariser       focused ABOUT_OLAKE excerpt for this query
  → deep_researcher                unified reasoning + retrieval
  → route by research_confidence:  >= 0.8 → solution_provider
                                   0.5–<0.8 → clarification_asker
                                   < 0.5 → escalation_handler
  → END
"""

from langgraph.graph import StateGraph, END
from typing import Literal, Callable, Any, Dict

from agent.state import ConversationState
from agent.nodes.context_builder import build_context
from agent.nodes.olake_context_summariser import summarise_olake_context
from agent.nodes.deep_researcher import deep_researcher
from agent.nodes.solution_provider import solution_provider
from agent.nodes.clarification_asker import clarification_asker_sync
from agent.nodes.escalation_handler import escalation_handler
from agent.logger import get_logger


# ---------------------------------------------------------------------------
# Step logging: which node ran and what it produced (for logs + test_agent callback)
# ---------------------------------------------------------------------------

def _step_summary(node_name: str, state: ConversationState) -> Dict[str, Any]:
    """Build a short result summary for a node (for logging and pretty-print)."""
    summary: Dict[str, Any] = {}
    if node_name == "build_context":
        summary["org_member_replied"] = state.get("org_member_replied", False)
        thread = state.get("thread_context") or []
        summary["thread_messages"] = len(thread)
    elif node_name == "olake_context_summariser":
        summary["needs_codebase_search"] = state.get("needs_codebase_search")
        raw = state.get("about_olake_summary") or ""
        summary["about_olake_chars"] = len(raw)
    elif node_name == "deep_researcher":
        summary["research_confidence"] = state.get("research_confidence")
        files = state.get("research_files") or []
        summary["research_files_count"] = len(files)
        summary["research_iterations"] = state.get("research_iterations", 0)
    elif node_name == "solution":
        resp = state.get("response_text") or ""
        summary["response_length"] = len(resp)
        summary["final_confidence"] = state.get("final_confidence")
    elif node_name == "clarification_asker":
        qs = state.get("clarification_questions") or []
        summary["clarification_questions_count"] = len(qs)
        resp = state.get("response_text") or ""
        summary["response_length"] = len(resp)
    elif node_name == "escalation_handler":
        summary["escalation_reason"] = (state.get("escalation_reason") or "")[:80]
        resp = state.get("response_text") or ""
        summary["response_length"] = len(resp)
    return summary


def _wrap_node(node_name: str, node_fn: Callable[[ConversationState], ConversationState]):
    """Wrap a node so we log step start/end and optionally notify a callback (e.g. test_agent)."""

    def wrapped(state: ConversationState) -> ConversationState:
        logger = get_logger()
        callback = state.get("_step_log_callback")
        try:
            logger.log_step_start(node_name)
            if callback:
                try:
                    callback("start", node_name, None)
                except Exception:
                    pass
            result = node_fn(state)
            summary = _step_summary(node_name, result)
            logger.log_step_end(node_name, summary=summary)
            if callback:
                try:
                    callback("end", node_name, summary)
                except Exception:
                    pass
            return result
        except Exception as e:
            err_msg = str(e)
            logger.log_step_end(node_name, summary=None, error=err_msg)
            if callback:
                try:
                    callback("end", node_name, {"error": err_msg})
                except Exception:
                    pass
            raise

    return wrapped


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------

def route_after_context(
    state: ConversationState,
) -> Literal["deep_researcher", "__end__"]:
    """After context build: exit silently if an org member is in the thread."""
    if state.get("org_member_replied"):
        get_logger().logger.info("Org member in thread — bot staying silent.")
        return "__end__"
    return "olake_context_summariser"


def route_after_summariser(
    state: ConversationState,
) -> Literal["deep_researcher", "solution"]:
    """After summariser: if question needs codebase search go to deep_researcher, else directly to solution."""
    if state.get("needs_codebase_search", True):
        return "deep_researcher"
    return "solution"


def route_after_research(
    state: ConversationState,
) -> Literal["solution", "clarification_asker", "escalation_handler"]:
    """Route by research_confidence: >= 0.8 → solution, 0.5–<0.8 → clarify, < 0.5 → escalate."""
    conf = state.get("research_confidence", 0.0)
    if conf >= 0.8:
        return "solution"
    if conf >= 0.5:
        return "clarification_asker"
    return "escalation_handler"


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

def create_agent_graph() -> StateGraph:
    """
    Build and compile the LangGraph agent workflow.

    Node sequence:
      build_context → olake_context_summariser → deep_researcher
      → (by research_confidence) solution | clarification_asker | escalation_handler → END
    """
    logger = get_logger()
    logger.logger.info("Creating agent graph...")

    workflow = StateGraph(ConversationState)

    # ── Nodes (wrapped with step logging: which agent ran + result summary) ───
    workflow.add_node("build_context", _wrap_node("build_context", build_context))
    workflow.add_node("olake_context_summariser", _wrap_node("olake_context_summariser", summarise_olake_context))
    workflow.add_node("deep_researcher", _wrap_node("deep_researcher", deep_researcher))
    workflow.add_node("solution", _wrap_node("solution", solution_provider))
    workflow.add_node("clarification_asker", _wrap_node("clarification_asker", clarification_asker_sync))
    workflow.add_node("escalation_handler", _wrap_node("escalation_handler", escalation_handler))

    # ── Entry ─────────────────────────────────────────────────────────────
    workflow.set_entry_point("build_context")

    # After context: exit silently if org member in thread; else run summariser
    workflow.add_conditional_edges(
        "build_context",
        route_after_context,
        {
            "olake_context_summariser": "olake_context_summariser",
            "__end__": END,
        },
    )
    # After summariser: codebase search needed → deep_researcher; generic question → solution directly
    workflow.add_conditional_edges(
        "olake_context_summariser",
        route_after_summariser,
        {
            "deep_researcher": "deep_researcher",
            "solution": "solution",
        },
    )

    # After research: route by research_confidence (0–1)
    workflow.add_conditional_edges(
        "deep_researcher",
        route_after_research,
        {
            "solution": "solution",
            "clarification_asker": "clarification_asker",
            "escalation_handler": "escalation_handler",
        },
    )
    workflow.add_edge("solution", END)
    workflow.add_edge("clarification_asker", END)
    workflow.add_edge("escalation_handler", END)

    compiled = workflow.compile()
    logger.logger.info("Agent graph created successfully")
    return compiled


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_graph = None


def get_agent_graph():
    """Get or create the global agent graph (compiled once per process)."""
    global _graph
    if _graph is None:
        _graph = create_agent_graph()
    return _graph
