"""
LangGraph workflow for OLake Slack Community Agent (production).

Used by main.py in production and by test_agent.py for local testing — keep
both in sync so test runs reflect prod behavior.

Topology:
  build_context
       └─ [org member in thread] → END silently
  → deep_researcher                unified reasoning + retrieval (analyze → search → evaluate → dig)
  → solution_provider | clarification | escalation | low_confidence_tagger
  → END
"""

from langgraph.graph import StateGraph, END
from typing import Literal

from agent.state import ConversationState
from agent.nodes.intent_analyzer import analyze_intent_sync
from agent.nodes.context_builder import build_context
from agent.nodes.deep_researcher import deep_researcher
from agent.nodes.solution_provider import solution_provider
from agent.nodes.clarification_asker import clarification_asker_sync
from agent.nodes.escalation_handler import escalation_handler
from agent.nodes.low_confidence_tagger import low_confidence_tagger
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
) -> Literal["solution", "clarification", "escalation", "low_confidence_tagger"]:
    """
    Route after deep_researcher completes.
    
    The researcher either found enough context (→ solution) or needs clarification.
    """
    if state.get("should_escalate"):
        return "escalation"
    
    # Check if research found sufficient context
    research_done = state.get("research_done", False)
    confidence = state.get("research_confidence", 0.0)
    files_found = len(state.get("research_files", []))
    
    # If confidence is too low, tag as low confidence
    if confidence < 0.4 or files_found == 0:
        return "low_confidence_tagger"
    
    # If researcher determined clarification is needed
    if state.get("needs_clarification"):
        return "clarification"
    
    # Default to solution
    return "solution"


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

def create_agent_graph() -> StateGraph:
    """
    Build and compile the LangGraph agent workflow.

    Node sequence:
      build_context → deep_researcher → solution | clarification | escalation | low_confidence_tagger
    """
    logger = get_logger()
    logger.logger.info("Creating agent graph...")

    workflow = StateGraph(ConversationState)

    # ── Nodes ────────────────────────────────────────────────────────────
    workflow.add_node("build_context",     build_context)
    workflow.add_node("deep_researcher",   deep_researcher)
    workflow.add_node("solution",          solution_provider)
    workflow.add_node("clarification",     clarification_asker_sync)
    workflow.add_node("escalation",        escalation_handler)
    workflow.add_node("low_confidence_tagger", low_confidence_tagger)

    # ── Entry ─────────────────────────────────────────────────────────────
    workflow.set_entry_point("build_context")

    # After context: exit silently if org member is in the thread
    workflow.add_conditional_edges(
        "build_context",
        route_after_context,
        {
            "deep_researcher": "deep_researcher",
            "__end__": END,
        },
    )

    # After research: route to solution, clarification, or low_confidence_tagger
    workflow.add_conditional_edges(
        "deep_researcher",
        route_after_research,
        {
            "solution":      "solution",
            "clarification": "clarification",
            "escalation":    "escalation",
            "low_confidence_tagger": "low_confidence_tagger",
        },
    )

    workflow.add_edge("solution",      END)
    workflow.add_edge("clarification", END)
    workflow.add_edge("escalation",    END)
    workflow.add_edge("low_confidence_tagger", END)

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
