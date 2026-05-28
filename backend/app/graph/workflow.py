"""LangGraph workflow builder. See SPEC.md §5 for the node/edge contract.

Phase 1 implements:
  - `discover` node  → calls Librarian, writes candidates to state.
  - `await_pool_approval` gate — the HITL gate for Phase 1 approval.

Phase 2 implements:
  - `synthesize` node → calls Critic, writes matrix + summary to state.
  - `await_synthesis_approval` gate — the HITL gate for Phase 2 approval.

Phase 4 (`draft_section`) remains a stub; the graph still compiles so the
e2e flow can be validated end-to-end.

Approval gates use LangGraph's `interrupt()` — the graph pauses at the
interrupt point until an external `Command(resume=…)` is issued by the
workflow REST endpoint.
"""

from __future__ import annotations

from typing import Any, cast

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app.agents.critic import Critic, CriticInput
from app.agents.librarian import Librarian, LibrarianInput
from app.graph.state import GraphState
from app.models.schemas import Paper, Phase
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
        # Surface query-expansion LLM usage so _run_graph can write it to
        # audit_log and apply the cost cap (NFR-5) before requesting approval.
        "discovery_usage": result.usage,
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
    # Safer default: anything other than the literal "approve" is treated as
    # reject. Previously any non-"reject" string (including None, "", garbage)
    # silently advanced the graph — audit finding #6.
    if approval == "approve":
        _log.info("gate_pool_approved", project_id=str(state.get("project_id")))
        return {**state, "awaiting_approval": False, "pool_approval": "approve"}
    if approval != "reject":
        _log.warning(
            "gate_pool_unknown_resume",
            project_id=str(state.get("project_id")),
            value=str(approval)[:64],
        )
    _log.info("gate_pool_rejected", project_id=str(state.get("project_id")))
    return {**state, "awaiting_approval": False, "pool_approval": "reject"}


async def node_synthesize(state: GraphState) -> GraphState:
    """Run the Critic agent over the approved pool — Phase 2 synthesis.

    Before invoking the Critic, the full-text fetcher downloads open-access
    PDFs (Semantic Scholar / arXiv / known OA mirrors), parses them with
    ``pypdf``, chunks the text and pushes chunks into the project's ChromaDB
    namespace. The Critic's existing ``vector_store.query`` calls then surface
    real paper content as RAG context instead of just abstracts (BRD FR-1.2).

    Full-text ingestion is best-effort — any failure logs a warning and the
    Critic falls back to abstract-only extraction (matches the rest of the
    Phase 2 graceful-degradation contract).
    """
    _log.info("node_synthesize_start", project_id=str(state.get("project_id")))

    approved_raw = state.get("approved_pool", [])
    approved_papers = [Paper(**d) for d in approved_raw]

    # Best-effort full-text ingestion → ChromaDB. Errors must not sink the run.
    # Step (a): Unpaywall enrichment — for any paper without a pdf_url that
    # carries a DOI, look up a legal OA PDF URL. This dramatically raises
    # full-text coverage for Crossref-only papers (they rarely come with PDFs).
    # Step (b): the fulltext fetcher downloads + parses + embeds chunks.
    project_id = state.get("project_id")
    if project_id is not None and approved_papers:
        try:
            from app.services.unpaywall import get_unpaywall_enricher

            approved_papers = await get_unpaywall_enricher().enrich(approved_papers)
            resolved = sum(1 for p in approved_papers if p.pdf_url is not None)
            _log.info(
                "unpaywall_enrich_done",
                project_id=str(project_id),
                resolved=resolved,
                pool_size=len(approved_papers),
            )
        except Exception as exc:  # never fail synthesis because of Unpaywall
            _log.warning("unpaywall_enrich_skipped", error=str(exc))

        try:
            from app.services.fulltext_fetcher import get_fulltext_fetcher

            ingested = await get_fulltext_fetcher().ingest(project_id, approved_papers)
            _log.info(
                "fulltext_ingest_done",
                project_id=str(project_id),
                ingested=ingested,
                pool_size=len(approved_papers),
            )
        except Exception as exc:  # never fail synthesis because of a PDF
            _log.warning("fulltext_ingest_skipped", error=str(exc))

    critic = Critic()
    result = await critic.run(
        CriticInput(
            approved_papers=approved_papers,
            focus=None,
            feedback=state.get("last_feedback"),
        )
    )

    _log.info("node_synthesize_done", paper_count=len(approved_papers))

    return {
        **state,
        "phase": Phase.SYNTHESIS,
        "matrix": result.matrix.model_dump(mode="json"),
        "summary": result.summary.model_dump(mode="json"),
        "synthesis_usage": result.usage.model_dump(mode="json"),
        "awaiting_approval": False,  # gate node sets this
    }


async def node_await_synthesis_approval(state: GraphState) -> GraphState:
    """HITL gate for Phase 2.

    Mirrors `node_await_pool_approval`: issues an `interrupt()` so LangGraph
    persists the checkpoint and suspends the graph until the
    `/workflow/{approve|reject|override}` endpoint resumes it with a Command.

    On `override` (SPEC §5.2/§5.3) the human-edited artifact carried in
    `last_override` becomes the *canonical* output of the synthesis node — it
    replaces the Critic's `matrix` or `summary` in state so drafting consumes
    the human version, not the agent's.
    """
    _log.info("gate_synthesis_approval_waiting", project_id=str(state.get("project_id")))

    approval = interrupt(
        {
            "phase": Phase.SYNTHESIS,
            "message": "Review and approve the literature synthesis.",
        }
    )

    # Match the same defensive default as the pool gate — only the literal
    # "approve" passes; everything else (including unexpected resume values)
    # is treated as reject (audit finding #6).
    if approval != "approve":
        if approval != "reject":
            _log.warning(
                "gate_synthesis_unknown_resume",
                project_id=str(state.get("project_id")),
                value=str(approval)[:64],
            )
        _log.info("gate_synthesis_rejected", project_id=str(state.get("project_id")))
        return {**state, "awaiting_approval": False, "synthesis_approval": "reject"}

    # Override: a manually-edited artifact replaces the agent output as the
    # canonical synthesis result (SPEC §5.3 — manual_override semantics).
    override = state.get("last_override")
    new_state: GraphState = {
        **state,
        "awaiting_approval": False,
        "synthesis_approval": "approve",
    }
    if override is not None:
        kind = override.get("kind") if isinstance(override, dict) else None
        if kind == "summary":
            new_state["summary"] = override
            _log.info("gate_synthesis_override_summary", project_id=str(state.get("project_id")))
        elif kind == "matrix":
            new_state["matrix"] = override
            _log.info("gate_synthesis_override_matrix", project_id=str(state.get("project_id")))
        else:
            # Unknown kind — previously this dropped the override silently
            # (audit finding #5). Log loudly so it's visible in the audit
            # trail, then still clear the field so a later gate doesn't
            # re-consume the same payload.
            _log.warning(
                "gate_synthesis_override_unknown_kind",
                project_id=str(state.get("project_id")),
                kind=str(kind)[:64],
            )
        # Clear it so a later gate does not re-consume the same override.
        new_state["last_override"] = None
        return new_state

    _log.info("gate_synthesis_approved", project_id=str(state.get("project_id")))
    return new_state


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


def _route_after_synthesis(state: GraphState) -> str:
    """After the synthesis gate, decide where to go.

    If rejected, loop back to `synthesize` (re-runs the Critic with feedback).
    Otherwise advance to drafting. Phase 3 (analyze) is out of MVP scope.
    """
    if state.get("synthesis_approval") == "reject":
        return NODE_SYNTHESIZE
    return NODE_DRAFT


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
    g.add_node(NODE_AWAIT_SYNTHESIS, node_await_synthesis_approval)
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

    # synthesize → await_synthesis_approval (always)
    g.add_edge(NODE_SYNTHESIZE, NODE_AWAIT_SYNTHESIS)

    # await_synthesis_approval → draft_section or back to synthesize
    g.add_conditional_edges(
        NODE_AWAIT_SYNTHESIS,
        _route_after_synthesis,
        {NODE_SYNTHESIZE: NODE_SYNTHESIZE, NODE_DRAFT: NODE_DRAFT},
    )

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
