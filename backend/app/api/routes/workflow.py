"""Workflow control routes. See SPEC.md §3.3."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.models.db import ProjectRow
from app.models.schemas import WorkflowRun
from app.services import workflow as wf_svc

router = APIRouter(tags=["workflow"], prefix="/projects/{project_id}/workflow")


# Per the security audit (see docs/audit/phase-2-audit.md):
# - feedback flows into LLM prompts; cap length to prevent prompt-injection
#   amplification and to keep us under the model's context window.
# - override content lands in the artifacts table and into LangGraph state;
#   cap to a sane reviewable size (~256 KB of markdown is plenty).
# - artifact_kind and mime_type are constrained to the SPEC §2.2 literals so
#   a crafted client cannot write garbage strings into the DB.

_MAX_FEEDBACK_CHARS = 2_000
_MAX_OVERRIDE_CONTENT_CHARS = 256_000
_MAX_LABEL_CHARS = 200
_MAX_MIME_CHARS = 100

# Artifact.kind literal from SPEC §2.2 — replicated here so the route can
# reject unknown values at the API boundary before they reach the DB.
ArtifactKindIn = Literal["matrix", "summary", "section", "figure", "code", "log"]


class FeedbackPayload(BaseModel):
    feedback: str | None = Field(default=None, max_length=_MAX_FEEDBACK_CHARS)


class OverridePayload(BaseModel):
    artifact_kind: ArtifactKindIn
    label: str = Field(..., min_length=1, max_length=_MAX_LABEL_CHARS)
    content: str = Field(..., min_length=1, max_length=_MAX_OVERRIDE_CONTENT_CHARS)
    mime_type: str = Field(default="text/markdown", max_length=_MAX_MIME_CHARS)


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
    """Submit a manually-edited artifact in place of the agent output.

    Writes an ArtifactRow (produced_by='human') and an audit entry
    before advancing the gate (SPEC §7.3).
    """
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")
    if run.state != "awaiting_approval":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "phase_locked", "message": "Workflow is not awaiting approval."},
        )
    return await wf_svc.override_workflow(
        db,
        project_id=project_id,
        run_id=run.id,
        user_id=user.id,
        artifact_kind=payload.artifact_kind,
        label=payload.label,
        content=payload.content,
        mime_type=payload.mime_type,
    )


@router.get("/candidates", response_model=list[dict[str, Any]])
async def list_candidates(
    project_id: UUID, user: CurrentUser, db: DbSession
) -> list[dict[str, Any]]:
    """Read candidate papers from the LangGraph checkpoint state.

    This avoids needing a separate DB persistence step — the graph state
    is the source of truth while the workflow is awaiting approval.
    """
    await _assert_project_owned(db, project_id, user.id)
    run = await wf_svc.get_active_run(db, project_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No active workflow run")

    graph = wf_svc.get_compiled_graph()
    config = {"configurable": {"thread_id": str(run.id)}}
    snapshot = await graph.aget_state(config)
    candidates: list[dict[str, Any]] = snapshot.values.get("candidates", [])
    return candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_project_owned(db: DbSession, project_id: UUID, user_id: UUID) -> None:
    row = await db.get(ProjectRow, project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project {project_id} not found")
    if row.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your project")
