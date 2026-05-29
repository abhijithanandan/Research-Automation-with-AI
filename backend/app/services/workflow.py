"""Workflow orchestration service.

Sits between the REST routes and LangGraph. Owns:
- Creating / resuming `WorkflowRun` DB rows.
- Dispatching the compiled graph.
- Broadcasting WS events.
- Writing audit log entries.

The graph instance is compiled once at startup (held in `_compiled_graph`).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import HTTPException, status
from langgraph.types import Command
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from typing_extensions import TypedDict

from app.db.session import flush_for_background_dispatch
from app.graph.state import GraphState
from app.graph.workflow import NODE_AWAIT_SECTION, NODE_AWAIT_SYNTHESIS, build_graph
from app.models.db import ArtifactRow, AuditLogRow, PaperRow, ProjectRow, WorkflowRunRow
from app.models.schemas import VALID_RUN_STATES, Paper, Phase, WorkflowRun
from app.utils.logging import get_logger

# Module-level set to keep strong references to background tasks (prevents GC).
_background_tasks: set[asyncio.Task[None]] = set()

_log = get_logger(__name__)

# Module-level compiled graph — initialised in lifespan startup.
_compiled_graph: Any = None

# Multi-subscriber event bus: project_id → set of queues (one per connected WS client).
# Using a set allows multiple tabs / reconnections without clobbering each other.
_ws_event_bus: dict[UUID, set[asyncio.Queue[dict[str, object]]]] = {}

# Last significant event per project — replayed to late WS subscribers so
# they catch approval.required / state.changed even if they connected after
# the event fired (race condition between workflow/start and WS connect).
_REPLAY_TYPES = {"approval.required", "state.changed", "agent.error", "cost.cap_exceeded"}
_last_event: dict[UUID, dict[str, object]] = {}


def get_compiled_graph() -> Any:
    if _compiled_graph is None:
        raise RuntimeError("Graph not initialised. Call init_graph() in lifespan.")
    return _compiled_graph


async def init_graph(checkpointer: Any) -> None:
    """Compile the graph once. Called from the FastAPI lifespan hook."""
    global _compiled_graph
    _compiled_graph = build_graph(checkpointer)
    _log.info("graph_compiled")


# ---------------------------------------------------------------------------
# WS event bus helpers
# ---------------------------------------------------------------------------


def subscribe_project(project_id: UUID) -> asyncio.Queue[dict[str, object]]:
    """Register a new per-connection event queue for project_id.

    Multiple subscribers (tabs, reconnections) are supported — each gets its
    own queue. The last significant event is replayed immediately so late
    subscribers catch up regardless of when they connect.
    """
    q: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=256)
    _ws_event_bus.setdefault(project_id, set()).add(q)
    # Replay the last significant event so late subscribers are not stuck.
    cached = _last_event.get(project_id)
    if cached is not None:
        try:
            q.put_nowait(cached)
        except asyncio.QueueFull:
            pass
    return q


def unsubscribe_project(project_id: UUID, q: asyncio.Queue[dict[str, object]]) -> None:
    """Remove a specific queue from the subscriber set for project_id."""
    queues = _ws_event_bus.get(project_id)
    if queues is not None:
        queues.discard(q)
        if not queues:
            del _ws_event_bus[project_id]


async def _emit(project_id: UUID, event: dict[str, object]) -> None:
    """Fan-out an event to all subscribers for project_id.

    Also caches the event if it is a significant type so late WS subscribers
    can be replayed when they connect.
    """
    event["ts"] = datetime.now(tz=UTC).isoformat()
    if event.get("type") in _REPLAY_TYPES:
        _last_event[project_id] = event
    for q in list(_ws_event_bus.get(project_id, set())):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning("ws_queue_full", project_id=str(project_id))


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------


async def _write_audit(
    session: AsyncSession,
    *,
    project_id: UUID,
    workflow_run_id: UUID | None,
    actor: str,
    action: str,
    payload: dict[str, object],
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Append an audit_log row. The model/token/cost columns are optional —
    populated for agent LLM calls so the per-project usage rollup and cost cap
    (BRD FR-3.3, NFR-5) can be computed."""
    entry = AuditLogRow(
        id=uuid4(),
        project_id=project_id,
        workflow_run_id=workflow_run_id,
        actor=actor,
        action=action,
        payload=payload,
        model=model,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        created_at=datetime.now(tz=UTC),
    )
    session.add(entry)


# ---------------------------------------------------------------------------
# Cost cap enforcement (BRD NFR-5)
# ---------------------------------------------------------------------------


async def _enforce_cost_cap(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
) -> bool:
    """Roll up the project's spend and enforce the per-project cost cap.

    Sums ``audit_log.cost_usd`` for the project and compares it against
    ``projects.token_cap_usd``. Behaviour (BRD NFR-5):

      * spend >= cap            → emit ``cost.cap_exceeded``, write a
        ``cost.cap_exceeded`` audit row, and return ``True`` so the caller
        stops the workflow instead of spending further.
      * spend >= warn_pct * cap → emit ``cost.cap_warn`` once we cross the
        threshold (best-effort; the client shows a banner) and return
        ``False`` (workflow continues).
      * otherwise               → return ``False``.

    The check runs in the same background session that just wrote the
    agent's usage row, so the rollup includes the call that may have pushed
    us over. Returns a bool rather than raising because the callers are
    background gate handlers, not request paths — they translate the signal
    into a state transition + WS event.
    """
    from sqlalchemy import func

    from app.config import get_settings

    project = await session.get(ProjectRow, project_id)
    if project is None:
        return False
    cap = float(project.token_cap_usd or 0.0)
    if cap <= 0.0:
        # A non-positive cap means "uncapped" — never enforce.
        return False

    spend = (
        await session.execute(
            select(func.coalesce(func.sum(AuditLogRow.cost_usd), 0.0)).where(
                AuditLogRow.project_id == project_id
            )
        )
    ).scalar_one()
    spend = float(spend or 0.0)

    warn_pct = get_settings().token_cap_warn_pct

    if spend >= cap:
        await _write_audit(
            session,
            project_id=project_id,
            workflow_run_id=run_id,
            actor="system",
            action="cost.cap_exceeded",
            payload={"spend_usd": spend, "cap_usd": cap},
        )
        await _emit(
            project_id,
            {
                "type": "cost.cap_exceeded",
                "run_id": str(run_id),
                "spend_usd": spend,
                "cap_usd": cap,
            },
        )
        return True

    if spend >= warn_pct * cap:
        await _emit(
            project_id,
            {
                "type": "cost.cap_warn",
                "run_id": str(run_id),
                "spend_usd": spend,
                "cap_usd": cap,
                "warn_pct": warn_pct,
            },
        )
    return False


# ---------------------------------------------------------------------------
# Paper persistence helper
# ---------------------------------------------------------------------------


async def _persist_candidates(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
    papers: list[Paper],
) -> None:
    """Upsert candidate papers into the `papers` table.

    Atomic per (project_id, citation_key) via PostgreSQL ``ON CONFLICT DO
    NOTHING``. Closes the check-then-insert race that previously could create
    duplicate rows when two discovery runs raced on the same project (audit
    round-3, CRIT-2). The unique constraint on (project_id, citation_key) is
    enforced by Alembic revision 0002.

    All rows are written with approved=False regardless of input
    (invariant from docs/agents/librarian.md §Invariants).
    """
    if not papers:
        return
    now = datetime.now(tz=UTC)
    rows = [
        {
            "id": paper.id,
            "project_id": project_id,
            "source": paper.source,
            "external_id": paper.external_id,
            "title": paper.title,
            "authors": list(paper.authors),
            "year": paper.year,
            "abstract": paper.abstract,
            "pdf_url": str(paper.pdf_url) if paper.pdf_url else None,
            "citation_key": paper.citation_key,
            "citation_count": paper.citation_count,
            "approved": False,  # invariant — never trust input
            "added_at": now,
        }
        for paper in papers
    ]
    # Use dialect-native ON CONFLICT so the insert stays atomic per row and
    # the test path (SQLite) and prod path (Postgres) exercise the same
    # semantics. SQLite supports ON CONFLICT since 3.24 (aiosqlite ships a
    # recent SQLite).
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        pg_stmt = (
            pg_insert(PaperRow)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["project_id", "citation_key"],
            )
        )
        await session.execute(pg_stmt)
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        sqlite_stmt = (
            sqlite_insert(PaperRow)
            .values(rows)
            .on_conflict_do_nothing(
                index_elements=["project_id", "citation_key"],
            )
        )
        await session.execute(sqlite_stmt)
    _ = run_id  # reserved for future audit linkage


async def _persist_artifacts(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
    artifacts: list[dict[str, Any]],
) -> None:
    """Insert Critic-produced artifacts (matrix, summary) into the `artifacts` table.

    Atomic per artifact id via dialect-native ``ON CONFLICT DO NOTHING`` on
    the primary key (coderabbit PR #5 finding). The previous get-then-add
    pattern was non-atomic: under concurrent persists (a retry racing with
    the original) it could raise IntegrityError. With ON CONFLICT the second
    write is a no-op, matching the idempotency semantics of
    :func:`_persist_candidates`.
    """
    if not artifacts:
        return
    now = datetime.now(tz=UTC)
    rows = [
        {
            "id": UUID(str(art["id"])),
            "project_id": project_id,
            "kind": str(art["kind"]),
            "label": str(art["label"]),
            "content": str(art["content"]),
            "mime_type": str(art["mime_type"]),
            "produced_by": str(art["produced_by"]),
            "created_at": now,
        }
        for art in artifacts
    ]
    dialect = session.bind.dialect.name if session.bind is not None else "postgresql"
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        pg_stmt = pg_insert(ArtifactRow).values(rows).on_conflict_do_nothing(index_elements=["id"])
        await session.execute(pg_stmt)
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        sqlite_stmt = (
            sqlite_insert(ArtifactRow).values(rows).on_conflict_do_nothing(index_elements=["id"])
        )
        await session.execute(sqlite_stmt)
    _ = run_id  # reserved for future audit linkage


# ---------------------------------------------------------------------------
# Workflow operations
# ---------------------------------------------------------------------------


async def start_workflow(
    session: AsyncSession,
    project_id: UUID,
    user_id: UUID,
) -> WorkflowRun:
    """Create a WorkflowRun row and kick off the graph."""
    # Load project to get seed_query.
    project = await session.get(ProjectRow, project_id)
    if project is None:
        raise ValueError(f"Project {project_id} not found")

    # Check for an existing active run. The partial unique index
    # `uq_workflow_runs_active_project` (alembic 0004) makes this insert
    # race-safe at the DB layer: even if two concurrent start_workflow
    # calls both see no existing row, only the first INSERT succeeds —
    # the second raises IntegrityError which we catch below and resolve
    # by re-fetching the winner's row.
    async def _fetch_active_run() -> WorkflowRunRow | None:
        return (
            (
                await session.execute(
                    select(WorkflowRunRow).where(
                        WorkflowRunRow.project_id == project_id,
                        WorkflowRunRow.state.in_(["running", "awaiting_approval"]),
                    )
                )
            )
            .scalars()
            .first()
        )

    existing = await _fetch_active_run()
    if existing is not None:
        _log.info("workflow_resume", run_id=str(existing.id))
        return _run_to_schema(existing)

    now = datetime.now(tz=UTC)
    run = WorkflowRunRow(
        id=uuid4(),
        project_id=project_id,
        phase=Phase.DISCOVERY.value,
        state="running",
        checkpoint_id=str(uuid4()),
        started_at=now,
        last_event_at=now,
    )
    session.add(run)
    try:
        await session.flush()  # get the id before committing
    except IntegrityError:
        # Lost the race: a concurrent caller inserted an active run for
        # this project between our SELECT and INSERT. Roll back the failed
        # add, re-fetch the winner, and return that — observationally
        # identical to the "existing is not None" branch above.
        await session.rollback()
        winner = await _fetch_active_run()
        if winner is None:
            # The active run vanished (state moved to approved/error)
            # between the IntegrityError and the re-fetch. Re-raise so
            # the caller surfaces a clear error rather than silently
            # returning None.
            raise
        _log.info("workflow_race_lost_resumed", run_id=str(winner.id))
        return _run_to_schema(winner)

    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run.id,
        actor="system",
        action="workflow.start",
        payload={"user_id": str(user_id)},
    )

    # Update project status.
    await session.execute(
        update(ProjectRow)
        .where(ProjectRow.id == project_id)
        .values(status="active", updated_at=now)
    )

    # Make the WorkflowRunRow visible to _run_graph's fresh session before
    # we hand off. Named helper instead of bare session.commit() — see
    # app/db/session.py docstring for the contract (audit round-3, MED-2).
    await flush_for_background_dispatch(session)

    # Dispatch the graph in the background so the HTTP response returns quickly.
    # Keep a strong reference to prevent the task being GC'd before completion.
    task = asyncio.create_task(_run_graph(run.id, project_id, project.seed_query))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return _run_to_schema(run)


async def _run_graph(
    run_id: UUID,
    project_id: UUID,
    seed_query: str,
) -> None:
    """Execute the graph for the given run. Runs as a background task."""
    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}
    initial_state: GraphState = {
        "project_id": project_id,
        "workflow_run_id": run_id,
        "seed_query": seed_query,
        "phase": Phase.DISCOVERY,
        "candidates": [],
        "approved_pool": [],
        "awaiting_approval": False,
        "last_feedback": None,
        "last_override": None,
        "expanded_queries": [],
        "sections_done": [],
        "sections_remaining": [
            "abstract",
            "introduction",
            "related_work",
            "methodology",
            "results",
            "discussion",
            "conclusion",
        ],
        "drafts": [],
        "matrix": None,
        "summary": None,
    }

    # Late import — avoids circular imports at module load time.
    from app.db.session import get_session

    try:
        await _emit(
            project_id, {"type": "agent.started", "agent": "librarian", "run_id": str(run_id)}
        )
        await graph.ainvoke(initial_state, config)

        # ainvoke returns (does not raise) when the graph hits interrupt().
        # Persist the candidate papers then update DB state to awaiting_approval.
        graph_state = await graph.aget_state(config)
        candidates_raw: list[dict[str, Any]] = graph_state.values.get("candidates", [])
        candidate_papers = [Paper(**d) for d in candidates_raw]

        # Token/cost rollup from the Librarian's query-expansion call.
        usage: dict[str, Any] = graph_state.values.get("discovery_usage") or {}

        capped = False
        async with get_session() as bg_session:
            await _persist_candidates(bg_session, project_id, run_id, candidate_papers)
            await _update_run_state(bg_session, run_id, "awaiting_approval")
            # Record the Librarian's LLM usage so the per-project cost cap
            # (NFR-5) sees Phase-1 spend, mirroring _handle_gate_pause for
            # Phase 2 and _handle_section_gate_pause for Phase 4.
            if usage:
                await _write_audit(
                    bg_session,
                    project_id=project_id,
                    workflow_run_id=run_id,
                    actor="librarian",
                    action="agent.invoke",
                    payload={"agent": "librarian", "llm_calls": 1},
                    model=usage.get("model"),
                    tokens_in=usage.get("tokens_in"),
                    tokens_out=usage.get("tokens_out"),
                    cost_usd=usage.get("cost_usd"),
                )
            capped = await _enforce_cost_cap(bg_session, project_id, run_id)
            if capped:
                await _update_run_state(bg_session, run_id, "error")

        await _emit(
            project_id,
            {
                "type": "agent.completed",
                "agent": "librarian",
                "run_id": str(run_id),
                "artifact_ids": [],
            },
        )
        if capped:
            # _enforce_cost_cap already emitted cost.cap_exceeded; skip the
            # approval gate — the user must raise the cap before continuing.
            return
        await _emit(
            project_id,
            {
                "type": "approval.required",
                "phase": Phase.DISCOVERY.value,
                "run_id": str(run_id),
                "summary": "Paper candidates are ready for your review.",
            },
        )
    except asyncio.CancelledError:
        # Lifespan shutdown / task cancellation — propagate, do NOT mark as error.
        raise
    except Exception as exc:
        # Background-task isolation: any uncaught exception here would crash the
        # task silently. We keep a broad catch but emit the exception *class* as
        # a structured field so incident diagnostics (round-4 MED-4) aren't
        # reduced to a single free-text "error" string.
        # GraphInterrupt is handled by LangGraph internally (ainvoke returns,
        # not raises) — anything reaching here is a genuine failure.
        error_code = type(exc).__name__
        _log.error(
            "graph_run_error",
            run_id=str(run_id),
            error_code=error_code,
            error=str(exc),
            exc_info=True,
        )
        async with get_session() as bg_session:
            await _update_run_state(bg_session, run_id, "error")
        await _emit(
            project_id,
            {
                "type": "agent.error",
                "agent": "librarian",
                "run_id": str(run_id),
                "error_code": error_code,
                "error": str(exc),
            },
        )


async def _update_run_state(
    session: AsyncSession,
    run_id: UUID,
    new_state: str,
    new_phase: Phase | None = None,
) -> None:
    """Update WorkflowRun.state and (optionally) WorkflowRun.phase.

    The ``phase`` column used to drift from reality — it stayed at the
    initial "discovery" forever, even after the graph moved into synthesis
    and drafting. That broke phase-dependent enforcement (paper-pool lock,
    UI status) on long-running projects. Callers that know the post-transition
    phase pass it here; callers that don't (mid-state updates) omit it.
    """
    # Guardrail (audit P0): reject any out-of-contract state literal at the one
    # chokepoint every state write goes through. This is what would have caught
    # the orphan-cleanup "failed" bug. VALID_RUN_STATES is the single source of
    # truth (app/models/schemas.py), kept in sync with the WorkflowRun.state
    # Literal.
    if new_state not in VALID_RUN_STATES:
        raise ValueError(
            f"Refusing to write invalid workflow run state {new_state!r}; "
            f"must be one of {sorted(VALID_RUN_STATES)}."
        )
    now = datetime.now(tz=UTC)
    values: dict[str, object] = {"state": new_state, "last_event_at": now}
    if new_state == "awaiting_approval":
        values["awaiting_since"] = now
    if new_phase is not None:
        values["phase"] = new_phase.value
    await session.execute(
        update(WorkflowRunRow).where(WorkflowRunRow.id == run_id).values(**values)
    )
    # No commit here — callers own the transaction:
    # _run_graph uses get_session() which auto-commits on clean exit;
    # route handlers use DbSession whose get_session() also auto-commits.


# Phase machine — what each HITL gate transitions into on approve/override.
# Used by both approve_workflow and override_workflow so the DB run.phase
# tracks the LangGraph state machine (MED-1 reviewer finding).
_NEXT_PHASE_AFTER_GATE: dict[str, Phase] = {
    Phase.DISCOVERY.value: Phase.SYNTHESIS,
    Phase.SYNTHESIS.value: Phase.DRAFTING,
    Phase.DRAFTING.value: Phase.DONE,
}


async def record_citation_correction(
    session: AsyncSession,
    *,
    project_id: UUID,
    run_id: UUID,
    user_id: UUID,
    label: str,
    corrections: dict[str, str],
    reason: str | None,
) -> None:
    """Audit a human citation correction (FR-1.5) as ``user.citation_correction``.

    The actual content rewrite happens in the route (via
    citations.apply_citation_corrections); this records the *decision* so the
    audit appendix shows exactly which keys the human changed and why.
    """
    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="user",
        action="user.citation_correction",
        payload={
            "user_id": str(user_id),
            "section": label,
            "corrections": corrections,
            "reason": reason,
        },
    )


class DraftingTelemetry(TypedDict):
    sections_drafted: int
    regenerations: int
    overrides: int
    citation_corrections: int
    avg_section_ms: int | None


async def drafting_telemetry(session: AsyncSession, project_id: UUID) -> DraftingTelemetry:
    """Phase-4 telemetry rollup from audit_log (NFR-6 / BRD §9). No new table.

    - sections_drafted     = count of ``phase_4.section_ready`` rows.
    - regenerations        = ``user.reject`` rows whose payload.phase=='drafting'.
    - overrides            = count of ``user.override`` rows.
    - citation_corrections = count of ``user.citation_correction`` rows.
    - avg_section_ms       = mean of ``draft_ms`` over section_ready rows.

    The audit log per project is small, so the reject/phase filter and the
    draft_ms mean are computed in Python rather than with dialect-specific
    JSON SQL — keeps it portable across SQLite (tests) and Postgres (prod).
    """
    rows = (
        await session.execute(
            select(AuditLogRow.action, AuditLogRow.payload).where(
                AuditLogRow.project_id == project_id,
                AuditLogRow.action.in_(
                    [
                        "phase_4.section_ready",
                        "user.reject",
                        "user.override",
                        "user.citation_correction",
                    ]
                ),
            )
        )
    ).all()

    sections_drafted = 0
    regenerations = 0
    overrides = 0
    citation_corrections = 0
    draft_ms_values: list[int] = []
    for action, payload in rows:
        data = payload if isinstance(payload, dict) else {}
        if action == "phase_4.section_ready":
            sections_drafted += 1
            ms = data.get("draft_ms")
            if isinstance(ms, int):
                draft_ms_values.append(ms)
        elif action == "user.reject":
            if data.get("phase") == "drafting":
                regenerations += 1
        elif action == "user.override":
            overrides += 1
        elif action == "user.citation_correction":
            citation_corrections += 1

    avg_section_ms = round(sum(draft_ms_values) / len(draft_ms_values)) if draft_ms_values else None
    return {
        "sections_drafted": sections_drafted,
        "regenerations": regenerations,
        "overrides": overrides,
        "citation_corrections": citation_corrections,
        "avg_section_ms": avg_section_ms,
    }


async def approve_workflow(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
    user_id: UUID,
    feedback: str | None = None,
    *,
    forced_unresolved: bool = False,
    override_reason: str | None = None,
) -> WorkflowRun:
    """Resume the graph with an approve command.

    Hydrates state["approved_pool"] from DB-approved papers (SPEC §5.2, §6.2)
    and writes a phase_1.approved_pool audit entry for the audit trail.
    Graph resume is dispatched to a background task so the HTTP handler returns
    immediately without blocking on the next phase's LLM calls.
    """
    run = await _assert_awaiting(session, run_id)

    # Build the per-phase resume payload + audit shape.
    resume_update: dict[str, Any] = {}
    phase_specific_audit: dict[str, Any] | None = None

    if run.phase == Phase.DISCOVERY.value:
        # Phase 1: hydrate approved_pool from DB-toggled papers.
        approved_rows = (
            (
                await session.execute(
                    select(PaperRow).where(
                        PaperRow.project_id == project_id,
                        PaperRow.approved.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        approved_pool = [
            {
                "id": str(r.id),
                "project_id": str(r.project_id),
                "source": r.source,
                "external_id": r.external_id,
                "title": r.title,
                "authors": list(r.authors),
                "year": r.year,
                "abstract": r.abstract,
                "pdf_url": r.pdf_url,
                "citation_key": r.citation_key,
                "citation_count": r.citation_count,
                "approved": True,
                "added_at": r.added_at.isoformat(),
            }
            for r in approved_rows
        ]
        citation_keys = [r.citation_key for r in approved_rows]
        resume_update["approved_pool"] = approved_pool
        phase_specific_audit = {
            "action": "phase_1.approved_pool",
            "payload": {"citation_keys": citation_keys, "count": len(citation_keys)},
        }

    # Phase change rule:
    #   - Discovery approve → SYNTHESIS (one approval).
    #   - Synthesis approve → DRAFTING (one approval).
    #   - Drafting approve  → stays DRAFTING until the last section, then DONE.
    #     The "is last section" check happens inside the graph (node_assemble
    #     emits state.changed{phase:"done"} via the resume terminal branch).
    #     Here we simply *do not* advance the run.phase column — the next
    #     section gate will set it back to awaiting_approval/drafting via
    #     _handle_section_gate_pause; the manuscript-assembled branch sets
    #     it to DONE.
    if run.phase == Phase.DRAFTING.value:
        next_phase = None  # keep DRAFTING; advance to DONE happens in resume
    else:
        next_phase = _NEXT_PHASE_AFTER_GATE.get(run.phase)

    await _update_run_state(session, run_id, "approved", new_phase=next_phase)
    approve_payload: dict[str, object] = {
        "user_id": str(user_id),
        "feedback": feedback,
        "phase": run.phase,
    }
    # FR-1.5 audited escape hatch: if the reviewer knowingly approved a section
    # with unresolved citations, record the bypass + their reason so it is
    # fully traceable in the audit appendix.
    if forced_unresolved:
        approve_payload["forced_unresolved"] = True
        approve_payload["override_reason"] = override_reason
    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="user",
        action="user.approve",
        payload=approve_payload,
    )
    if phase_specific_audit is not None:
        # The phase_1.approved_pool audit row is the canonical proof that
        # the pool was approved (audit-marker layer in
        # _assert_phase_not_locked). Alembic 0006 enforces uniqueness per
        # workflow_run_id; if a second concurrent approve races through
        # _assert_awaiting and lands here, the partial unique index will
        # raise IntegrityError on flush. Translate that to a clear 409 so
        # callers don't get a 500.
        await _write_audit(
            session,
            project_id=project_id,
            workflow_run_id=run_id,
            actor="system",
            action=phase_specific_audit["action"],
            payload=phase_specific_audit["payload"],
        )
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            _log.info("workflow_double_approve_blocked", run_id=str(run_id))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "already_approved",
                    "message": "This pool has already been approved.",
                },
            ) from exc
    # Make state visible to the background resume task. Named helper per the
    # audit-round-3 commit-ownership contract (see app/db/session.py).
    await flush_for_background_dispatch(session)

    # Dispatch graph resume to background — the next phase may involve long LLM calls.
    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}
    task = asyncio.create_task(
        _resume_graph(
            project_id,
            run_id,
            graph,
            config,
            Command(resume="approve", update=resume_update),
            "approved",
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    # Return the freshly updated run state. Re-read so we surface the new
    # state/phase the route just committed.
    refreshed = await session.get(WorkflowRunRow, run_id)
    assert refreshed is not None  # _assert_awaiting already verified existence
    return _run_to_schema(refreshed)


async def override_workflow(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
    user_id: UUID,
    artifact_kind: str,
    label: str,
    content: str,
    mime_type: str = "text/markdown",
) -> WorkflowRun:
    """Submit a manually-edited artifact and advance the gate.

    Writes an ArtifactRow (produced_by='human') and an audit entry
    (action='user.override'), then resumes the graph in the background with the
    artifact injected into state['last_override'] (SPEC §7.3).
    """
    run = await _assert_awaiting(session, run_id)

    now = datetime.now(tz=UTC)
    artifact = ArtifactRow(
        id=uuid4(),
        project_id=project_id,
        kind=artifact_kind,
        label=label,
        content=content,
        mime_type=mime_type,
        produced_by="human",
        created_at=now,
    )
    session.add(artifact)
    await session.flush()

    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="user",
        action="user.override",
        payload={
            "user_id": str(user_id),
            "artifact_id": str(artifact.id),
            "artifact_kind": artifact_kind,
            "label": label,
        },
    )

    artifact_state = {
        "id": str(artifact.id),
        "project_id": str(project_id),
        "kind": artifact_kind,
        "label": label,
        "content": content,
        "mime_type": mime_type,
        "produced_by": "human",
        "parent_id": None,
        "created_at": now.isoformat(),
    }

    # An override advances the gate just like an approve. For Phase 4 the
    # phase column stays at DRAFTING (a section override doesn't necessarily
    # mean the manuscript is done — there may still be sections left). The
    # transition to DONE is handled by the resume terminal branch when the
    # graph reaches node_assemble.
    if run.phase == Phase.DRAFTING.value:
        next_phase = None
    else:
        next_phase = _NEXT_PHASE_AFTER_GATE.get(run.phase)
    await _update_run_state(session, run_id, "approved", new_phase=next_phase)
    # Flush for background resume — named helper per the commit-ownership contract.
    await flush_for_background_dispatch(session)

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}
    task = asyncio.create_task(
        _resume_graph(
            project_id,
            run_id,
            graph,
            config,
            Command(resume="approve", update={"last_override": artifact_state}),
            "approved",
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    refreshed = await session.get(WorkflowRunRow, run_id)
    assert refreshed is not None  # _assert_awaiting already verified existence
    return _run_to_schema(refreshed)


async def reject_workflow(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
    user_id: UUID,
    feedback: str,
) -> WorkflowRun:
    """Resume the graph with a reject command (re-runs discover with feedback).

    Graph resume is dispatched to a background task so the HTTP handler
    returns immediately.
    """
    await _assert_awaiting(session, run_id)

    # Capture the phase at reject time so telemetry can distinguish a drafting
    # regeneration (NFR-6 / §9 "regenerate count per section") from a
    # discovery/synthesis reject. Read before the state flip below.
    rejecting_run = await session.get(WorkflowRunRow, run_id)
    reject_phase = rejecting_run.phase if rejecting_run is not None else None

    await _update_run_state(session, run_id, "rejected")
    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="user",
        action="user.reject",
        payload={"user_id": str(user_id), "feedback": feedback, "phase": reject_phase},
    )
    # Flush for background reject task — named helper per the commit-ownership contract.
    await flush_for_background_dispatch(session)

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}
    task = asyncio.create_task(
        _resume_graph(
            project_id,
            run_id,
            graph,
            config,
            Command(resume="reject", update={"last_feedback": feedback}),
            "running",
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    run = await session.get(WorkflowRunRow, run_id)
    return _run_to_schema(run)  # type: ignore[arg-type]


async def get_active_run(session: AsyncSession, project_id: UUID) -> WorkflowRunRow | None:
    result = await session.execute(
        select(WorkflowRunRow)
        .where(WorkflowRunRow.project_id == project_id)
        .order_by(WorkflowRunRow.started_at.desc())
        .limit(1)
    )
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_run_in_state(
    session: AsyncSession,
    run_id: UUID,
    expected_states: set[str],
) -> WorkflowRunRow:
    """Generalized run-state guard (M2-D).

    Raises:
        HTTPException 404: the run does not exist.
        HTTPException 409: the run exists but is in a state not in
            ``expected_states``. The error envelope carries
            ``code='phase_locked'`` so the frontend ``ApiError`` classifier
            categorizes it as a conflict (which it is — the workflow has
            moved past or before the point the caller expected).

    The previous ``_assert_awaiting`` helper hard-coded a single allowed
    state. Generalizing to a set lets future callers express richer
    contracts (e.g. ``{"running", "awaiting_approval"}`` for reads that
    are safe in either state) without each handler reimplementing the
    HTTPException shape.
    """
    run = await session.get(WorkflowRunRow, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"WorkflowRun {run_id} not found")
    if run.state not in expected_states:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "phase_locked",
                "message": (
                    f"WorkflowRun is in state '{run.state}'; expected one of "
                    f"{sorted(expected_states)}."
                ),
            },
        )
    return run


async def _assert_awaiting(session: AsyncSession, run_id: UUID) -> WorkflowRunRow:
    """Raise HTTP 409 if the run is not in awaiting_approval state (SPEC §7 rule 2).

    Thin wrapper around :func:`_assert_run_in_state` for the most common
    case. Kept as its own named function because every Phase 1/2/4 gate
    handler uses it — the call sites read better with the descriptive name.
    """
    return await _assert_run_in_state(session, run_id, {"awaiting_approval"})


async def _resume_graph(
    project_id: UUID,
    run_id: UUID,
    graph: Any,
    config: dict[str, Any],
    command: Command[Any],
    done_state: str,
) -> None:
    """Run graph.ainvoke in a background task and emit the right follow-up event.

    After resuming, the graph may *interrupt again* at the next HITL gate
    (e.g. the Phase 2 synthesis gate). We detect that by inspecting
    `graph.aget_state(...).next` — a non-empty tuple means the graph is parked
    at a gate. In that case we persist the produced artifacts, set the DB state
    back to `awaiting_approval`, and emit `approval.required` instead of
    `state.changed`.
    """
    # Late import mirrors the pattern used by _run_graph to avoid circular deps.
    from app.db.session import get_session

    try:
        await graph.ainvoke(command, config)
        snapshot = await graph.aget_state(config)

        if snapshot.next and snapshot.next[0] == NODE_AWAIT_SYNTHESIS:
            # Graph paused at the Phase 2 synthesis gate.
            await _handle_gate_pause(project_id, run_id, snapshot, get_session)
            return

        if snapshot.next and snapshot.next[0] == NODE_AWAIT_SECTION:
            # Graph paused at a Phase 4 per-section gate.
            await _handle_section_gate_pause(project_id, run_id, snapshot, get_session)
            return

        if snapshot.next:
            # Graph paused at a Phase 1 gate again (e.g. after a reject re-runs
            # discover). Phase 2 and Phase 4 gates are handled by the branches
            # above; anything reaching here is — by exclusion — a Phase 1
            # re-pause, so Phase.DISCOVERY is correct by construction.
            # (snapshot.next[0] is a *node name* like NODE_AWAIT_POOL_APPROVAL,
            # not a phase enum — see graph/workflow.py.)
            #
            # PR #5 fix: persist the freshly-discovered candidates before
            # re-arming the gate. On a reject-with-feedback the graph re-runs
            # `discover` and produces a *new* candidate set in the checkpoint;
            # the frontend reads the pool from GET /papers (the DB table), so
            # without this upsert the refined results never become visible and
            # the reject→refine cycle silently does nothing. Mirrors the
            # persist-then-await-gate sequence in _run_graph. ON CONFLICT DO
            # NOTHING keeps it idempotent for candidates that survived the
            # refinement.
            candidates_raw: list[dict[str, Any]] = snapshot.values.get("candidates", [])
            candidate_papers = [Paper(**d) for d in candidates_raw]
            async with get_session() as bg_session:
                await _persist_candidates(bg_session, project_id, run_id, candidate_papers)
                await _update_run_state(bg_session, run_id, "awaiting_approval")
            await _emit(
                project_id,
                {
                    "type": "approval.required",
                    "phase": Phase.DISCOVERY.value,
                    "run_id": str(run_id),
                    "summary": "Paper candidates are ready for your review.",
                },
            )
            return

        # The graph ran to completion (no further gate). With Phase 4 wired,
        # a populated `manuscript` in state means we reached node_assemble
        # and the project is done — persist the manuscript artifact and
        # emit phase="done". Otherwise this is the Phase-1/2 terminal path
        # which we keep for backward compat.
        manuscript = snapshot.values.get("manuscript")
        if manuscript:
            async with get_session() as bg_session:
                await _persist_artifacts(bg_session, project_id, run_id, [manuscript])
                await _update_run_state(bg_session, run_id, "approved", Phase.DONE)
                await _write_audit(
                    bg_session,
                    project_id=project_id,
                    workflow_run_id=run_id,
                    actor="system",
                    action="phase_4.manuscript_assembled",
                    payload={"manuscript_id": str(manuscript.get("id"))},
                )
            await _emit(
                project_id,
                {
                    "type": "agent.completed",
                    "agent": "scribe",
                    "run_id": str(run_id),
                    "artifact_ids": [str(manuscript.get("id"))],
                },
            )
            await _emit(
                project_id,
                {
                    "type": "state.changed",
                    "phase": Phase.DONE.value,
                    "state": "approved",
                    "run_id": str(run_id),
                },
            )
            return

        ws_state = "approved" if done_state == "approved" else "running"
        ws_phase = Phase.SYNTHESIS.value if done_state == "approved" else Phase.DISCOVERY.value
        # Persist the terminal transition BEFORE emitting state.changed
        # (coderabbit PR #5 finding). Previously the WS event told clients
        # "approved" while workflow_runs.state stayed at "running" —
        # downstream consumers reading the DB would see stale state.
        async with get_session() as bg_session:
            await _update_run_state(
                bg_session,
                run_id,
                ws_state,
                Phase(ws_phase),
            )
        await _emit(
            project_id,
            {
                "type": "state.changed",
                "phase": ws_phase,
                "state": ws_state,
                "run_id": str(run_id),
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # See MED-4 note on the sibling handler in _run_graph: keep the broad
        # catch (background-task isolation) but emit the exception class as a
        # structured error_code so incidents are diagnosable from logs/UI.
        error_code = type(exc).__name__
        _log.error(
            "graph_resume_error",
            run_id=str(run_id),
            error_code=error_code,
            error=str(exc),
            exc_info=True,
        )
        async with get_session() as bg_session:
            await _update_run_state(bg_session, run_id, "error")
        await _emit(
            project_id,
            {
                # "system" is the catch-all agent id used by the WS contract
                # when a failure cannot be attributed to a specific agent —
                # at resume time we don't know whether discover or synthesize
                # raised (coderabbit PR #5 finding: payload must include agent
                # for the WS contract to stay consistent).
                "type": "agent.error",
                "agent": "system",
                "run_id": str(run_id),
                "error_code": error_code,
                "error": str(exc),
            },
        )


async def _handle_gate_pause(
    project_id: UUID,
    run_id: UUID,
    snapshot: Any,
    get_session: Any,
) -> None:
    """Persist Phase 2 artifacts and re-arm the gate after a synthesis pause."""
    values = snapshot.values
    matrix = values.get("matrix")
    summary = values.get("summary")
    artifacts = [a for a in (matrix, summary) if a is not None]
    # Token/cost rollup the Critic accumulated across its LLM calls.
    usage: dict[str, Any] = values.get("synthesis_usage") or {}

    capped = False
    async with get_session() as bg_session:
        if artifacts:
            await _persist_artifacts(bg_session, project_id, run_id, artifacts)
        await _update_run_state(bg_session, run_id, "awaiting_approval")
        await _write_audit(
            bg_session,
            project_id=project_id,
            workflow_run_id=run_id,
            actor="system",
            action="phase_2.synthesis_ready",
            payload={"artifact_count": len(artifacts)},
        )
        # Record the Critic's LLM usage so per-project token/cost rollups and
        # the cost cap can be computed (BRD FR-3.3, §4.3 audit trail, NFR-5).
        if usage:
            await _write_audit(
                bg_session,
                project_id=project_id,
                workflow_run_id=run_id,
                actor="critic",
                action="agent.invoke",
                payload={
                    "agent": "critic",
                    "llm_calls": usage.get("llm_calls", 0),
                },
                model=usage.get("model"),
                tokens_in=usage.get("tokens_in"),
                tokens_out=usage.get("tokens_out"),
                cost_usd=usage.get("cost_usd"),
            )
        # Enforce the per-project cost cap (NFR-5). If we're over budget, halt
        # here rather than re-arming the gate — the synthesis the user is about
        # to review is the last work we'll do until they raise the cap.
        capped = await _enforce_cost_cap(bg_session, project_id, run_id)
        if capped:
            await _update_run_state(bg_session, run_id, "error")

    artifact_ids = [str(a["id"]) for a in artifacts if a.get("id")]
    await _emit(
        project_id,
        {
            "type": "agent.completed",
            "agent": "critic",
            "run_id": str(run_id),
            "artifact_ids": artifact_ids,
        },
    )
    if capped:
        # _enforce_cost_cap already emitted cost.cap_exceeded; don't ask the
        # user to approve a phase whose run we just moved to "error".
        return
    await _emit(
        project_id,
        {
            "type": "approval.required",
            "phase": Phase.SYNTHESIS.value,
            "run_id": str(run_id),
            "summary": "The literature synthesis is ready for your review.",
        },
    )


async def _handle_section_gate_pause(
    project_id: UUID,
    run_id: UUID,
    snapshot: Any,
    get_session: Any,
) -> None:
    """Persist the just-drafted section artifact and re-arm the Phase-4 gate.

    Called from ``_resume_graph`` when the graph parks at
    ``NODE_AWAIT_SECTION`` after ``node_draft_section`` has appended a fresh
    draft to ``state["drafts"]``.
    """
    values = snapshot.values
    section = values.get("current_section")
    drafts = values.get("drafts") or []
    # The just-drafted section is the entry with matching label — fall back
    # to the last entry if the label match misses (defence-in-depth).
    latest = None
    for d in reversed(drafts):
        if isinstance(d, dict) and d.get("section") == section:
            latest = d
            break
    if latest is None and drafts:
        latest = drafts[-1]
    artifact = latest.get("artifact") if isinstance(latest, dict) else None

    # Token/cost rollup the Scribe accumulated drafting this section.
    usage: dict[str, Any] = values.get("drafting_usage") or {}

    capped = False
    async with get_session() as bg_session:
        if artifact is not None:
            await _persist_artifacts(bg_session, project_id, run_id, [artifact])
        await _update_run_state(bg_session, run_id, "awaiting_approval", Phase.DRAFTING)
        section_ready_payload: dict[str, Any] = {"section": section}
        # Per-section drafting latency (NFR-6 / §9). node_draft_section stashes
        # it on the usage dict; surface it so /usage can roll up avg_section_ms.
        if "draft_ms" in usage:
            section_ready_payload["draft_ms"] = usage["draft_ms"]
        await _write_audit(
            bg_session,
            project_id=project_id,
            workflow_run_id=run_id,
            actor="system",
            action="phase_4.section_ready",
            payload=section_ready_payload,
        )
        # Record the Scribe's LLM usage so the per-project cost cap (NFR-5)
        # sees Phase-4 spend, mirroring the Critic usage write in
        # _handle_gate_pause.
        if usage:
            await _write_audit(
                bg_session,
                project_id=project_id,
                workflow_run_id=run_id,
                actor="scribe",
                action="agent.invoke",
                payload={
                    "agent": "scribe",
                    "section": section,
                    "llm_calls": usage.get("llm_calls", 0),
                },
                model=usage.get("model"),
                tokens_in=usage.get("tokens_in"),
                tokens_out=usage.get("tokens_out"),
                cost_usd=usage.get("cost_usd"),
            )
        capped = await _enforce_cost_cap(bg_session, project_id, run_id)
        if capped:
            await _update_run_state(bg_session, run_id, "error")

    artifact_ids = [str(artifact["id"])] if artifact and artifact.get("id") else []
    await _emit(
        project_id,
        {
            "type": "agent.completed",
            "agent": "scribe",
            "run_id": str(run_id),
            "artifact_ids": artifact_ids,
        },
    )
    if capped:
        return
    await _emit(
        project_id,
        {
            "type": "approval.required",
            "phase": Phase.DRAFTING.value,
            "section": section,
            "run_id": str(run_id),
            "summary": f"The {section} section is ready for your review.",
        },
    )


def _run_to_schema(run: WorkflowRunRow) -> WorkflowRun:
    return WorkflowRun(
        id=run.id,
        project_id=run.project_id,
        phase=Phase(run.phase),
        state=run.state,  # type: ignore[arg-type]
        checkpoint_id=run.checkpoint_id,
        started_at=run.started_at,
        awaiting_since=run.awaiting_since,
        last_event_at=run.last_event_at,
    )
