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
from typing import Literal

from agent.state import ConversationState
from agent.nodes.context_builder import build_context
from agent.nodes.olake_context_summariser import summarise_olake_context
from agent.nodes.deep_researcher import deep_researcher
from agent.nodes.solution_provider import solution_provider
from agent.nodes.clarification_asker import clarification_asker_sync
from agent.nodes.escalation_handler import escalation_handler
from agent.logger import get_logger


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
    return "deep_researcher"


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

    # ── Nodes ────────────────────────────────────────────────────────────
    workflow.add_node("build_context", build_context)
    workflow.add_node("olake_context_summariser", summarise_olake_context)
    workflow.add_node("deep_researcher", deep_researcher)
    workflow.add_node("solution", solution_provider)
    workflow.add_node("clarification_asker", clarification_asker_sync)
    workflow.add_node("escalation_handler", escalation_handler)

    # ── Entry ─────────────────────────────────────────────────────────────
    workflow.set_entry_point("build_context")

    # After context: exit silently if org member in thread; else summarise then research
    workflow.add_conditional_edges(
        "build_context",
        route_after_context,
        {
            "deep_researcher": "olake_context_summariser",
            "__end__": END,
        },
    )
    workflow.add_edge("olake_context_summariser", "deep_researcher")

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
