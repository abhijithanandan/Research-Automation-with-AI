"""Paper-pool routes. See SPEC.md §3.4."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.models.schemas import Paper

router = APIRouter(tags=["papers"], prefix="/projects/{project_id}/papers")


class UpdatePaperRequest(BaseModel):
    approved: bool | None = None
    citation_key: str | None = None
    title: str | None = None


@router.get("", response_model=list[Paper])
async def list_papers(project_id: UUID, user: CurrentUser) -> list[Paper]:
    _ = project_id, user
    return []


@router.post("/upload", response_model=Paper, status_code=status.HTTP_201_CREATED)
async def upload_paper(project_id: UUID, file: UploadFile, user: CurrentUser) -> Paper:
    _ = project_id, file, user
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "scaffold")


@router.patch("/{paper_id}", response_model=Paper)
async def update_paper(
    project_id: UUID, paper_id: UUID, payload: UpdatePaperRequest, user: CurrentUser
) -> Paper:
    _ = project_id, paper_id, payload, user
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "scaffold")


@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_paper(project_id: UUID, paper_id: UUID, user: CurrentUser) -> None:
    _ = project_id, paper_id, user
    return None
