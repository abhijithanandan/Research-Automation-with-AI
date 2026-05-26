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
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import flush_for_background_dispatch
from app.graph.state import GraphState
from app.graph.workflow import NODE_AWAIT_SYNTHESIS, build_graph
from app.models.db import ArtifactRow, AuditLogRow, PaperRow, ProjectRow, WorkflowRunRow
from app.models.schemas import Paper, Phase, WorkflowRun
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
_REPLAY_TYPES = {"approval.required", "state.changed", "agent.error"}
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

    # Check for an existing active run.
    existing = (
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
    await session.flush()  # get the id before committing

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
        await _emit(
            project_id,
            {
                "type": "agent.completed",
                "agent": "librarian",
                "run_id": str(run_id),
                "artifact_ids": [],
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


async def approve_workflow(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
    user_id: UUID,
    feedback: str | None = None,
) -> WorkflowRun:
    """Resume the graph with an approve command.

    Hydrates state["approved_pool"] from DB-approved papers (SPEC §5.2, §6.2)
    and writes a phase_1.approved_pool audit entry for the audit trail.
    Graph resume is dispatched to a background task so the HTTP handler returns
    immediately without blocking on the next phase's LLM calls.
    """
    run = await _assert_awaiting(session, run_id)

    # Build the approved pool from DB — these are the papers the user toggled
    # via PATCH /papers/{id} before calling approve.
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

    # Approving a gate transitions the workflow into the next phase. From
    # discovery → synthesis; from synthesis → drafting; etc. Persist the
    # phase change so the paper-lock rule and UI status see consistent state.
    next_phase = _NEXT_PHASE_AFTER_GATE.get(run.phase)
    await _update_run_state(session, run_id, "approved", new_phase=next_phase)
    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="user",
        action="user.approve",
        payload={"user_id": str(user_id), "feedback": feedback},
    )
    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="system",
        action="phase_1.approved_pool",
        payload={"citation_keys": citation_keys, "count": len(citation_keys)},
    )
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
            Command(resume="approve", update={"approved_pool": approved_pool}),
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

    # An override advances the gate just like an approve — bump the persisted
    # phase to the next phase in the state machine (MED-1 reviewer finding).
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

    await _update_run_state(session, run_id, "rejected")
    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="user",
        action="user.reject",
        payload={"user_id": str(user_id), "feedback": feedback},
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


async def _assert_awaiting(session: AsyncSession, run_id: UUID) -> WorkflowRunRow:
    """Raise HTTP 409 if the run is not in awaiting_approval state (SPEC §7 rule 2)."""
    run = await session.get(WorkflowRunRow, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"WorkflowRun {run_id} not found")
    if run.state != "awaiting_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "phase_locked",
                "message": f"WorkflowRun is in state '{run.state}', not 'awaiting_approval'.",
            },
        )
    return run


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

        if snapshot.next:
            # Graph paused at a Phase 1 gate again (e.g. after a reject re-runs discover).
            # DB state is already updated to awaiting_approval by the caller; just re-emit
            # the correct phase-1 approval.required event.
            async with get_session() as bg_session:
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

        # The graph ran to completion (no further gate).
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
    await _emit(
        project_id,
        {
            "type": "approval.required",
            "phase": Phase.SYNTHESIS.value,
            "run_id": str(run_id),
            "summary": "The literature synthesis is ready for your review.",
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
