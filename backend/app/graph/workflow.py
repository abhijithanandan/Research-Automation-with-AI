"""LangGraph workflow builder. See SPEC.md §5 for the node/edge contract.

Phase 1 implements:
  - `discover` node  → calls Librarian, writes candidates to state.
  - Interrupt before `synthesize` — the HITL gate for Phase 1 approval.

Phase 2 (`synthesize`) and Phase 4 (`draft_section`) nodes remain stubs;
the graph still compiles so the e2e flow can be validated end-to-end.

Approval gates use LangGraph's `interrupt()` — the graph pauses at the
interrupt point until an external `Command(resume=…)` is issued by the
workflow REST endpoint.
"""

from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app.agents.librarian import Librarian, LibrarianInput
from app.graph.state import GraphState
from app.models.schemas import Phase
from app.utils.logging import get_logger

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Node name constants — always reference these, never raw strings
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------


async def node_discover(state: GraphState) -> GraphState:
    """Run the Librarian agent and populate `candidates` in the state.

    Emitting `agent.started` / `agent.completed` WebSocket events is handled
    by the WS event bus (wired in the workflow service layer). The node itself
    only mutates state — it has no I/O side effects beyond the LLM + HTTP calls
    encapsulated inside the Librarian.
    """
    _log.info(
        "node_discover_start",
        project_id=str(state.get("project_id")),
    )

    librarian = Librarian()
    result = await librarian.run(
        LibrarianInput(
            seed_query=state.get("seed_query", ""),
            project_id=state.get("project_id"),
        )
    )

    _log.info("node_discover_done", candidate_count=len(result.candidates))

    return {
        **state,
        "phase": Phase.DISCOVERY,
        "candidates": [p.model_dump(mode="json") for p in result.candidates],
        "expanded_queries": result.expanded_queries,
        "awaiting_approval": False,  # gate node sets this
    }


async def node_await_pool_approval(state: GraphState) -> GraphState:
    """HITL gate for Phase 1.

    Persists a checkpoint, then issues an interrupt. The graph is suspended
    here until the `/workflow/approve` (or /reject) endpoint resumes it with
    a `Command`. This satisfies SPEC.md §5.3 gate invariants — the checkpoint
    is written *before* the interrupt is emitted.
    """
    _log.info("gate_pool_approval_waiting", project_id=str(state.get("project_id")))

    # `interrupt()` raises `GraphInterrupt` internally — LangGraph persists
    # the checkpoint before raising, then re-enters this node with the
    # resume value when the graph is commanded to continue.
    approval = interrupt(
        {
            "phase": Phase.DISCOVERY,
            "message": "Review and approve the candidate paper pool.",
        }
    )

    # On resume, `approval` carries the action string: "approve" | "reject".
    if approval == "reject":
        _log.info("gate_pool_rejected", project_id=str(state.get("project_id")))
        return {**state, "awaiting_approval": False, "pool_approval": "reject"}

    _log.info("gate_pool_approved", project_id=str(state.get("project_id")))
    return {**state, "awaiting_approval": False, "pool_approval": "approve"}


async def node_synthesize(state: GraphState) -> GraphState:
    """Phase 2 stub — Critic agent (implemented in v0.1 Phase 2 PR)."""
    _log.info("node_synthesize_stub", project_id=str(state.get("project_id")))
    return {**state, "phase": Phase.SYNTHESIS, "awaiting_approval": True}


async def node_draft_section(state: GraphState) -> GraphState:
    """Phase 4 stub — Scribe agent (implemented in Phase 4 PR)."""
    _log.info("node_draft_section_stub", project_id=str(state.get("project_id")))
    remaining = list(state.get("sections_remaining", []))
    if remaining:
        remaining.pop(0)
    return {
        **state,
        "phase": Phase.DRAFTING,
        "sections_remaining": remaining,
        "awaiting_approval": True,
    }


async def node_assemble(state: GraphState) -> GraphState:
    """Assembles all sections into a final manuscript artifact (Phase 4)."""
    _log.info("node_assemble_stub", project_id=str(state.get("project_id")))
    return state


# ---------------------------------------------------------------------------
# Edge routing helpers
# ---------------------------------------------------------------------------


def _route_after_pool(state: GraphState) -> str:
    """After the approval gate, decide where to go.

    If the gate was rejected (pool_approval == "reject"),
    loop back to discover. Otherwise advance to synthesize.
    """
    # If rejected the gate node returns awaiting_approval=False without
    # advancing the phase — we detect that and re-run discover.
    if state.get("pool_approval") == "reject":
        return NODE_DISCOVER
    return NODE_SYNTHESIZE


def _route_after_section(state: GraphState) -> str:
    remaining = state.get("sections_remaining", [])
    if remaining:
        return NODE_DRAFT
    return NODE_ASSEMBLE


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(checkpointer: Any) -> Any:
    """Construct and compile the LangGraph state machine.

    `checkpointer` must be an AsyncPostgresSaver (or MemorySaver for tests).
    The caller (lifespan hook or workflow service) is responsible for
    initialising and closing the checkpointer connection pool.

    Phase 1 interrupts before `NODE_SYNTHESIZE` — the graph pauses there
    after `node_await_pool_approval` hands control to LangGraph's interrupt
    mechanism.
    """
    g = StateGraph(GraphState)

    # Register nodes
    g.add_node(NODE_DISCOVER, node_discover)
    g.add_node(NODE_AWAIT_POOL, node_await_pool_approval)
    g.add_node(NODE_SYNTHESIZE, node_synthesize)
    g.add_node(NODE_DRAFT, node_draft_section)
    g.add_node(NODE_ASSEMBLE, node_assemble)

    # Entry point
    g.set_entry_point(NODE_DISCOVER)

    # discover → await_pool_approval (always)
    g.add_edge(NODE_DISCOVER, NODE_AWAIT_POOL)

    # await_pool_approval → synthesize or back to discover
    g.add_conditional_edges(
        NODE_AWAIT_POOL,
        _route_after_pool,
        {NODE_SYNTHESIZE: NODE_SYNTHESIZE, NODE_DISCOVER: NODE_DISCOVER},
    )

    # synthesize → draft_section (Phase 2 gate handled inside synthesize stub)
    g.add_edge(NODE_SYNTHESIZE, NODE_DRAFT)

    # draft_section → next section or assemble
    g.add_conditional_edges(
        NODE_DRAFT,
        _route_after_section,
        {NODE_DRAFT: NODE_DRAFT, NODE_ASSEMBLE: NODE_ASSEMBLE},
    )

    g.add_edge(NODE_ASSEMBLE, END)

    # Compile — using modern checkpointer. Node itself calls interrupt() internally,
    # avoiding redundant external double-interrupts.
    compiled = g.compile(
        checkpointer=checkpointer,
    )
    return compiled


# ---------------------------------------------------------------------------
# Checkpointer factory (used by the lifespan hook and tests)
# ---------------------------------------------------------------------------


async def create_postgres_checkpointer(
    database_url: str,
) -> Any:
    """Create a pool-backed AsyncPostgresSaver and run setup().

    Uses psycopg3's AsyncConnectionPool so connections stay alive across
    background asyncio tasks. The pool is owned by the caller — call
    `.conn.close()` on shutdown.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    _pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = cast(
        "AsyncConnectionPool[AsyncConnection[dict[str, Any]]]",
        AsyncConnectionPool(
            conninfo=pg_url,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row},
        ),
    )
    await _pool.open()
    saver = AsyncPostgresSaver(conn=_pool)
    await saver.setup()
    return saver
