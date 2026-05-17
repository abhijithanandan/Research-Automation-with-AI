"""Workflow control routes. See SPEC.md §3.3."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.models.db import ProjectRow
from app.models.schemas import WorkflowRun
from app.services import workflow as wf_svc

router = APIRouter(tags=["workflow"], prefix="/projects/{project_id}/workflow")


class FeedbackPayload(BaseModel):
    feedback: str | None = None


class OverridePayload(BaseModel):
    artifact_kind: str
    label: str
    content: str
    mime_type: str = "text/markdown"


@router.post("/start", response_model=WorkflowRun)
async def start_workflow(project_id: UUID, user: CurrentUser, db: DbSession) -> WorkflowRun:
    """Start or resume the workflow for a project."""
    await _assert_project_owned(db, project_id, user.id)
    return await wf_svc.start_workflow(db, project_id, user.id)


@router.get("", response_model=WorkflowRun)
async def get_workflow(project_id: UUID, user: CurrentUser, db: DbSession) -> WorkflowRun:
    """Get the active workflow run for a project."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    return wf_svc._run_to_schema(run)


@router.post("/approve", response_model=WorkflowRun)
async def approve(
    project_id: UUID, payload: FeedbackPayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Approve the pending phase and advance the workflow (SPEC.md §7)."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    if run.state != "awaiting_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "phase_locked", "message": "Workflow is not awaiting approval."},
        )
    return await wf_svc.approve_workflow(db, project_id, run.id, user.id, payload.feedback)


@router.post("/reject", response_model=WorkflowRun)
async def reject(
    project_id: UUID, payload: FeedbackPayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Reject the current phase output and re-run with feedback."""
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    if run.state != "awaiting_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "phase_locked", "message": "Workflow is not awaiting approval."},
        )
    feedback = payload.feedback or "Please refine the output."
    return await wf_svc.reject_workflow(db, project_id, run.id, user.id, feedback)


@router.post("/override", response_model=WorkflowRun)
async def override(
    project_id: UUID, payload: OverridePayload, user: CurrentUser, db: DbSession
) -> WorkflowRun:
    """Submit a manually-edited artifact in place of the agent output."""
    await _assert_project_owned(db, project_id, user.id)
    # Override treated as approve with a manual artifact recorded.
    # Full implementation wires the artifact write; for Phase 1 it delegates
    # to approve so the gate advances.
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    return await wf_svc.approve_workflow(db, project_id, run.id, user.id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_project_owned(db: DbSession, project_id: UUID, user_id: UUID) -> None:
    row = await db.get(ProjectRow, project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project {project_id} not found")
    if row.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your project")
