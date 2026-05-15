"""LangGraph workflow builder. See SPEC.md §5 for the node/edge contract.

This is the *skeleton*. Implementations of each node call into `app.agents.*`
and `app.services.*`. Approval gates use LangGraph's interrupt() semantics —
they pause the run until an external `command` resumes them.
"""

from __future__ import annotations

from typing import Any

from app.graph.state import GraphState


# Node names — kept here so they're referenced symbolically, not as strings.
NODE_DISCOVER = "discover"
NODE_AWAIT_POOL = "await_pool_approval"
NODE_SYNTHESIZE = "synthesize"
NODE_AWAIT_SYNTHESIS = "await_synthesis_approval"
NODE_ANALYZE = "analyze"  # v0.2
NODE_AWAIT_ANALYSIS = "await_analysis_approval"  # v0.2
NODE_DRAFT = "draft_section"
NODE_AWAIT_SECTION = "await_section_approval"
NODE_ASSEMBLE = "assemble"
NODE_DONE = "done"


async def node_discover(state: GraphState) -> GraphState:
    # TODO: call app.agents.librarian.Librarian.run; populate state["candidates"].
    return {**state, "awaiting_approval": True}


async def node_synthesize(state: GraphState) -> GraphState:
    # TODO: call Critic; populate matrix + summary.
    return {**state, "awaiting_approval": True}


async def node_draft_section(state: GraphState) -> GraphState:
    # TODO: call Scribe for state["sections_remaining"][0].
    return {**state, "awaiting_approval": True}


async def node_assemble(state: GraphState) -> GraphState:
    # TODO: concatenate drafts + bibliography into a final manuscript artifact.
    return state


def build_graph() -> Any:
    """Construct the LangGraph state machine.

    Pseudocode (real implementation pending):

        from langgraph.graph import StateGraph, END

        g = StateGraph(GraphState)
        g.add_node(NODE_DISCOVER, node_discover)
        g.add_node(NODE_SYNTHESIZE, node_synthesize)
        g.add_node(NODE_DRAFT, node_draft_section)
        g.add_node(NODE_ASSEMBLE, node_assemble)

        g.set_entry_point(NODE_DISCOVER)
        g.add_edge(NODE_DISCOVER, NODE_SYNTHESIZE)  # gate via interrupt_before
        g.add_edge(NODE_SYNTHESIZE, NODE_DRAFT)     # gate via interrupt_before
        g.add_conditional_edges(NODE_DRAFT, _route_after_section, {
            "next_section": NODE_DRAFT,
            "assemble": NODE_ASSEMBLE,
        })
        g.add_edge(NODE_ASSEMBLE, END)

        return g.compile(
            checkpointer=postgres_saver,
            interrupt_before=[NODE_SYNTHESIZE, NODE_DRAFT, NODE_ASSEMBLE],
        )
    """
    raise NotImplementedError("build_graph: implement when LangGraph wiring is finalized")
