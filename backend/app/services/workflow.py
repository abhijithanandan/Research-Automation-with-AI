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

from app.graph.state import GraphState
from app.graph.workflow import build_graph
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
) -> None:
    entry = AuditLogRow(
        id=uuid4(),
        project_id=project_id,
        workflow_run_id=workflow_run_id,
        actor=actor,
        action=action,
        payload=payload,
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

    Keyed by (project_id, citation_key) — idempotent on re-runs.
    All rows are written with approved=False regardless of input
    (invariant from docs/agents/librarian.md §Invariants).
    """
    now = datetime.now(tz=UTC)
    for paper in papers:
        # Check if a row with this (project_id, citation_key) already exists.
        existing = (
            await session.execute(
                select(PaperRow).where(
                    PaperRow.project_id == project_id,
                    PaperRow.citation_key == paper.citation_key,
                )
            )
        ).scalars().first()
        if existing is not None:
            continue
        row = PaperRow(
            id=paper.id,
            project_id=project_id,
            source=paper.source,
            external_id=paper.external_id,
            title=paper.title,
            authors=list(paper.authors),
            year=paper.year,
            abstract=paper.abstract,
            pdf_url=str(paper.pdf_url) if paper.pdf_url else None,
            citation_key=paper.citation_key,
            citation_count=paper.citation_count,
            approved=False,  # invariant — never trust input
            added_at=now,
        )
        session.add(row)


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

    # Commit now so _run_graph's fresh session sees the new WorkflowRunRow.
    # Without this, the background task could start before the row is visible.
    await session.commit()

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
        candidates_raw: list[dict] = graph_state.values.get("candidates", [])
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
            project_id, {"type": "agent.completed", "agent": "librarian", "run_id": str(run_id)}
        )
    except Exception as exc:
        # GraphInterrupt is handled by LangGraph internally (ainvoke returns, not raises).
        # Any exception reaching here is a genuine failure.
        _log.error("graph_run_error", run_id=str(run_id), error=str(exc))
        async with get_session() as bg_session:
            await _update_run_state(bg_session, run_id, "error")
        await _emit(
            project_id,
            {"type": "agent.error", "agent": "librarian", "run_id": str(run_id), "error": str(exc)},
        )


async def _update_run_state(session: AsyncSession, run_id: UUID, new_state: str) -> None:
    now = datetime.now(tz=UTC)
    values: dict[str, object] = {"state": new_state, "last_event_at": now}
    if new_state == "awaiting_approval":
        values["awaiting_since"] = now
    await session.execute(
        update(WorkflowRunRow).where(WorkflowRunRow.id == run_id).values(**values)
    )
    # No commit here — callers own the transaction:
    # _run_graph uses get_session() which auto-commits on clean exit;
    # route handlers use DbSession whose get_session() also auto-commits.


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
    await _assert_awaiting(session, run_id)

    # Build the approved pool from DB — these are the papers the user toggled
    # via PATCH /papers/{id} before calling approve.
    approved_rows = (
        await session.execute(
            select(PaperRow).where(
                PaperRow.project_id == project_id,
                PaperRow.approved.is_(True),
            )
        )
    ).scalars().all()

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

    await _update_run_state(session, run_id, "approved")
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
    # Commit so the background task sees the updated state.
    await session.commit()

    # Dispatch graph resume to background — the next phase may involve long LLM calls.
    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}
    task = asyncio.create_task(
        _resume_graph(project_id, run_id, graph, config,
                      Command(resume="approve", update={"approved_pool": approved_pool}),
                      "approved")
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    # Return the freshly updated run state.
    run = await session.get(WorkflowRunRow, run_id)
    return _run_to_schema(run)  # type: ignore[arg-type]


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
    await _assert_awaiting(session, run_id)

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

    await _update_run_state(session, run_id, "approved")
    await session.commit()

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}
    task = asyncio.create_task(
        _resume_graph(project_id, run_id, graph, config,
                      Command(resume="approve", update={"last_override": artifact_state}),
                      "approved")
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    run = await session.get(WorkflowRunRow, run_id)
    return _run_to_schema(run)  # type: ignore[arg-type]


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
    await session.commit()

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}
    task = asyncio.create_task(
        _resume_graph(project_id, run_id, graph, config,
                      Command(resume="reject"),
                      "running")
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
    command: Command,
    done_state: str,
) -> None:
    """Run graph.ainvoke in a background task and emit the completion event."""
    try:
        await graph.ainvoke(command, config)
        ws_state = "approved" if done_state == "approved" else "running"
        ws_phase = Phase.SYNTHESIS.value if done_state == "approved" else Phase.DISCOVERY.value
        await _emit(
            project_id,
            {
                "type": "state.changed",
                "phase": ws_phase,
                "state": ws_state,
                "run_id": str(run_id),
            },
        )
    except Exception as exc:
        _log.error("graph_resume_error", run_id=str(run_id), error=str(exc))
        # Late import mirrors the pattern used by _run_graph to avoid circular deps.
        from app.db.session import get_session
        async with get_session() as bg_session:
            await _update_run_state(bg_session, run_id, "error")
        await _emit(
            project_id,
            {"type": "agent.error", "run_id": str(run_id), "error": str(exc)},
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
