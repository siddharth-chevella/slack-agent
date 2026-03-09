"""
LangGraph workflow for CLI mode.

Simplified topology without Slack dependencies:
  cli_context_builder → deep_researcher → relevance_filter → cli_solution_provider | clarification | low_confidence → END
"""

from langgraph.graph import StateGraph, END
from typing import Literal

from agent.state import ConversationState
from agent.nodes.deep_researcher import deep_researcher
from agent.nodes.clarification_asker import clarification_asker_sync
from agent.nodes.cli import (
    build_cli_context,
    cli_solution_provider,
    cli_low_confidence_tagger,
    cli_relevance_filter,
)
from agent.logger import get_logger


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------

def route_after_research(
    state: ConversationState,
) -> Literal["solution", "clarification", "low_confidence"]:
    """
    Route after deep_researcher completes.
    """
    # Check if research found sufficient context
    confidence = state.get("research_confidence", 0.0)
    files_found = len(state.get("research_files", []))

    # If confidence is too low, use low confidence handler
    if confidence < 0.4 or files_found == 0:
        return "low_confidence"

    # If researcher determined clarification is needed
    if state.get("needs_clarification"):
        return "clarification"

    # Default to solution
    return "solution"


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

def create_cli_agent_graph() -> StateGraph:
    """
    Build and compile the LangGraph agent workflow for CLI mode.
    
    Node sequence:
      cli_context_builder → deep_researcher → relevance_filter → solution | clarification | low_confidence
    """
    logger = get_logger()
    logger.logger.info("Creating CLI agent graph...")
    
    workflow = StateGraph(ConversationState)
    
    # ── Nodes ────────────────────────────────────────────────────────────
    workflow.add_node("cli_context_builder", build_cli_context)
    workflow.add_node("deep_researcher", deep_researcher)
    workflow.add_node("relevance_filter", cli_relevance_filter)
    workflow.add_node("solution", cli_solution_provider)
    workflow.add_node("clarification", clarification_asker_sync)
    workflow.add_node("low_confidence", cli_low_confidence_tagger)

    # ── Entry ─────────────────────────────────────────────────────────────
    workflow.set_entry_point("cli_context_builder")

    # After context: always go to deep_researcher (no org member check in CLI)
    workflow.add_edge("cli_context_builder", "deep_researcher")

    # After research: filter relevance
    workflow.add_edge("deep_researcher", "relevance_filter")

    # After filtering: route to solution, clarification, or low_confidence
    workflow.add_conditional_edges(
        "relevance_filter",
        route_after_research,
        {
            "solution": "solution",
            "clarification": "clarification",
            "low_confidence": "low_confidence",
        },
    )
    
    workflow.add_edge("solution", END)
    workflow.add_edge("clarification", END)
    workflow.add_edge("low_confidence", END)
    
    compiled = workflow.compile()
    logger.logger.info("CLI agent graph created successfully")
    return compiled


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_cli_graph = None


def get_cli_agent_graph():
    """Get or create the global CLI agent graph (compiled once per process)."""
    global _cli_graph
    if _cli_graph is None:
        _cli_graph = create_cli_agent_graph()
    return _cli_graph
