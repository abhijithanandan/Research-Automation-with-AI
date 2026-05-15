"""Workflow control routes. See SPEC.md §3.3."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser

router = APIRouter(tags=["workflow"], prefix="/projects/{project_id}/workflow")


class FeedbackPayload(BaseModel):
    feedback: str | None = None


class OverridePayload(BaseModel):
    artifact_kind: str
    label: str
    content: str
    mime_type: str = "text/markdown"


@router.post("/start")
async def start_workflow(project_id: UUID, user: CurrentUser) -> dict[str, object]:
    """Start or resume the workflow. Returns the active WorkflowRun."""
    _ = project_id, user
    # TODO: locate-or-create WorkflowRun, dispatch graph, return state.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "scaffold")


@router.get("")
async def get_workflow(project_id: UUID, user: CurrentUser) -> dict[str, object]:
    _ = project_id, user
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "scaffold")


@router.post("/approve")
async def approve(
    project_id: UUID, payload: FeedbackPayload, user: CurrentUser
) -> dict[str, object]:
    """Approve the pending phase. See SPEC.md §7."""
    _ = project_id, payload, user
    # TODO: assert workflow.state == "awaiting_approval"; resume LangGraph; audit-log.
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "scaffold")


@router.post("/reject")
async def reject(
    project_id: UUID, payload: FeedbackPayload, user: CurrentUser
) -> dict[str, object]:
    _ = project_id, payload, user
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "scaffold")


@router.post("/override")
async def override(
    project_id: UUID, payload: OverridePayload, user: CurrentUser
) -> dict[str, object]:
    _ = project_id, payload, user
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "scaffold")
