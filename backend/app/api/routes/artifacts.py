"""Artifact + export routes. See SPEC.md §3.5."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser
from app.models.schemas import Artifact

router = APIRouter(tags=["artifacts"], prefix="/projects/{project_id}")


@router.get("/artifacts", response_model=list[Artifact])
async def list_artifacts(
    project_id: UUID, user: CurrentUser, kind: str | None = None
) -> list[Artifact]:
    _ = project_id, user, kind
    return []


@router.get("/artifacts/{artifact_id}", response_model=Artifact)
async def get_artifact(project_id: UUID, artifact_id: UUID, user: CurrentUser) -> Artifact:
    _ = project_id, artifact_id, user
    raise HTTPException(status.HTTP_404_NOT_FOUND, "not implemented")


@router.get("/export")
async def export_manuscript(
    project_id: UUID, user: CurrentUser, format: str = "markdown"
) -> dict[str, str]:
    _ = project_id, user
    if format not in {"markdown", "latex", "bibtex"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid format")
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "scaffold")
