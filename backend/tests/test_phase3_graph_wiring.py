"""Sprint-4 tests for the Phase-3 graph wiring + approval routes.

Covers the routing predicates and the new service functions. End-to-end
gate orchestration is exercised by the existing _resume_graph pathway via
the same LangGraph machinery the Phase-2 tests use; here we cover the
diff:

  1. The dataset-presence predicate at the synthesis gate routes into
     analyze_propose only when state["datasets"] is non-empty.
  2. The two new gate-internal routers (_route_after_code,
     _route_after_analysis) correctly fork on the resume token.
  3. The new build_graph registers exactly the expected new nodes.
"""

from __future__ import annotations

from app.graph.workflow import (
    NODE_ANALYZE_EXECUTE,
    NODE_ANALYZE_PROPOSE,
    NODE_AWAIT_ANALYSIS,
    NODE_AWAIT_CODE,
    NODE_DRAFT,
    NODE_SYNTHESIZE,
    _route_after_analysis,
    _route_after_code,
    _route_after_synthesis,
)


def test_synthesis_route_no_datasets_skips_to_drafting() -> None:
    """BRD §8: Phase 3 is opt-in. No datasets → straight to drafting."""
    state = {"synthesis_approval": "approve", "datasets": []}
    assert _route_after_synthesis(state) == NODE_DRAFT  # type: ignore[arg-type]


def test_synthesis_route_with_datasets_enters_phase3() -> None:
    """At least one dataset → graph routes into analyze_propose."""
    state = {
        "synthesis_approval": "approve",
        "datasets": [{"id": "abc", "filename": "x.csv"}],
    }
    assert _route_after_synthesis(state) == NODE_ANALYZE_PROPOSE  # type: ignore[arg-type]


def test_synthesis_reject_loops_back_to_synthesize_regardless_of_datasets() -> None:
    state = {
        "synthesis_approval": "reject",
        "datasets": [{"id": "abc"}],
    }
    assert _route_after_synthesis(state) == NODE_SYNTHESIZE  # type: ignore[arg-type]


def test_code_route_approve_executes() -> None:
    """The code-approval gate routes to analyze_execute on approve."""
    assert _route_after_code({"code_approval": "approve"}) == NODE_ANALYZE_EXECUTE  # type: ignore[arg-type]


def test_code_route_reject_regenerates() -> None:
    """Reject from the code gate loops back to analyze_propose (regen)."""
    assert _route_after_code({"code_approval": "reject"}) == NODE_ANALYZE_PROPOSE  # type: ignore[arg-type]


def test_analysis_route_approve_advances_to_drafting() -> None:
    """Approve at the post-execution gate advances to Phase 4."""
    assert _route_after_analysis({"analysis_approval": "approve"}) == NODE_DRAFT  # type: ignore[arg-type]


def test_analysis_route_reject_regenerates() -> None:
    """Reject from the results gate loops back to analyze_propose."""
    assert _route_after_analysis({"analysis_approval": "reject"}) == NODE_ANALYZE_PROPOSE  # type: ignore[arg-type]


def test_build_graph_registers_phase3_nodes() -> None:
    """Lock the new Phase-3 nodes into the build_graph contract."""
    from langgraph.checkpoint.memory import MemorySaver

    from app.graph.workflow import build_graph

    g = build_graph(MemorySaver())
    nodes = set(g.nodes) - {"__start__", "__end__"}
    assert NODE_ANALYZE_PROPOSE in nodes
    assert NODE_AWAIT_CODE in nodes
    assert NODE_ANALYZE_EXECUTE in nodes
    assert NODE_AWAIT_ANALYSIS in nodes
