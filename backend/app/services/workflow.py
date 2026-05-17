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

from langgraph.types import Command
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph.state import GraphState
from app.graph.workflow import build_graph
from app.models.db import AuditLogRow, ProjectRow, WorkflowRunRow
from app.models.schemas import Phase, WorkflowRun
from app.utils.logging import get_logger

_log = get_logger(__name__)

# Module-level compiled graph — initialised in lifespan startup.
_compiled_graph: Any = None
_ws_event_bus: dict[UUID, asyncio.Queue[dict[str, object]]] = {}


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
    """Create (or replace) a project-scoped event queue for a WS connection."""
    q: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=256)
    _ws_event_bus[project_id] = q
    return q


def unsubscribe_project(project_id: UUID) -> None:
    _ws_event_bus.pop(project_id, None)


async def _emit(project_id: UUID, event: dict[str, object]) -> None:
    """Put an event into the project's WS queue (if anyone is listening)."""
    event["ts"] = datetime.now(tz=UTC).isoformat()
    q = _ws_event_bus.get(project_id)
    if q is not None:
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

    # Dispatch the graph in the background so the HTTP response returns quickly.
    asyncio.create_task(  # noqa: RUF006
        _run_graph(run.id, project_id, project.seed_query, session)
    )

    return _run_to_schema(run)


async def _run_graph(
    run_id: UUID,
    project_id: UUID,
    seed_query: str,
    session: AsyncSession,
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

    try:
        await _emit(
            project_id, {"type": "agent.started", "agent": "librarian", "run_id": str(run_id)}
        )
        await graph.ainvoke(initial_state, config)
        # Graph interrupted — update state to awaiting_approval.
        await _update_run_state(session, run_id, "awaiting_approval")
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
        _log.error("graph_run_error", run_id=str(run_id), error=str(exc))
        await _update_run_state(session, run_id, "error")
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
    await session.commit()


async def approve_workflow(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
    user_id: UUID,
    feedback: str | None = None,
) -> WorkflowRun:
    """Resume the graph with an approve command."""
    run = await _assert_awaiting(session, run_id)

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}

    action = "reject" if feedback else "approve"
    await graph.ainvoke(Command(resume=action), config)

    await _update_run_state(session, run_id, "approved")
    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="user",
        action="user.approve",
        payload={"user_id": str(user_id), "feedback": feedback},
    )
    await _emit(
        project_id,
        {
            "type": "state.changed",
            "phase": Phase.SYNTHESIS.value,
            "state": "running",
            "run_id": str(run_id),
        },
    )
    return _run_to_schema(run)


async def reject_workflow(
    session: AsyncSession,
    project_id: UUID,
    run_id: UUID,
    user_id: UUID,
    feedback: str,
) -> WorkflowRun:
    """Resume the graph with a reject command (re-runs discover with feedback)."""
    run = await _assert_awaiting(session, run_id)

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": str(run_id)}}
    await graph.ainvoke(Command(resume="reject"), config)

    await _update_run_state(session, run_id, "rejected")
    await _write_audit(
        session,
        project_id=project_id,
        workflow_run_id=run_id,
        actor="user",
        action="user.reject",
        payload={"user_id": str(user_id), "feedback": feedback},
    )
    await _emit(
        project_id,
        {
            "type": "state.changed",
            "phase": Phase.DISCOVERY.value,
            "state": "running",
            "run_id": str(run_id),
        },
    )
    return _run_to_schema(run)


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
    """Raise if the run is not in awaiting_approval state (SPEC §7 rule 2)."""
    run = await session.get(WorkflowRunRow, run_id)
    if run is None:
        raise ValueError(f"WorkflowRun {run_id} not found")
    if run.state != "awaiting_approval":
        raise PermissionError(
            f"WorkflowRun {run_id} is in state '{run.state}', not 'awaiting_approval'."
        )
    return run


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
