"""
LangGraph workflow for CLI mode.

Simplified topology without Slack dependencies:
  cli_context_builder → olake_context_summariser → deep_researcher → relevance_filter → cli_solution_provider → END
"""

from langgraph.graph import StateGraph, END

from agent.state import ConversationState
from agent.nodes.olake_context_summariser import summarise_olake_context
from agent.nodes.deep_researcher import deep_researcher
from agent.nodes.cli import (
    build_cli_context,
    cli_solution_provider,
    cli_relevance_filter,
)
from agent.logger import get_logger


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------

def create_cli_agent_graph() -> StateGraph:
    """
    Build and compile the LangGraph agent workflow for CLI mode.

    Node sequence:
      cli_context_builder → deep_researcher → relevance_filter → solution
    """
    logger = get_logger()
    logger.logger.info("Creating CLI agent graph...")

    workflow = StateGraph(ConversationState)

    # ── Nodes ────────────────────────────────────────────────────────────
    workflow.add_node("cli_context_builder", build_cli_context)
    workflow.add_node("olake_context_summariser", summarise_olake_context)
    workflow.add_node("deep_researcher", deep_researcher)
    workflow.add_node("relevance_filter", cli_relevance_filter)
    workflow.add_node("solution", cli_solution_provider)

    # ── Entry ─────────────────────────────────────────────────────────────
    workflow.set_entry_point("cli_context_builder")

    # After context: summarise OLake context for this query, then research
    workflow.add_edge("cli_context_builder", "olake_context_summariser")
    workflow.add_edge("olake_context_summariser", "deep_researcher")

    # After research: filter relevance, then always to solution (answer/clarify/escalate)
    workflow.add_edge("deep_researcher", "relevance_filter")
    workflow.add_edge("relevance_filter", "solution")
    workflow.add_edge("solution", END)
    
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
