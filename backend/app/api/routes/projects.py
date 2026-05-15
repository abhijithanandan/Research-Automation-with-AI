"""Project CRUD routes. See SPEC.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.models.schemas import Phase, Project

router = APIRouter(tags=["projects"], prefix="/projects")


class CreateProjectRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    seed_query: str = Field(min_length=1, max_length=1000)
    output_format: str = "markdown"
    token_cap_usd: float = 5.0


class UpdateProjectRequest(BaseModel):
    title: str | None = None
    output_format: str | None = None
    token_cap_usd: float | None = None


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
async def create_project(payload: CreateProjectRequest, user: CurrentUser) -> Project:
    # TODO: persist via SQLAlchemy.
    now = datetime.now(tz=UTC)
    return Project(
        id=uuid4(),
        owner_id=user.id,
        title=payload.title,
        seed_query=payload.seed_query,
        output_format=payload.output_format,  # type: ignore[arg-type]
        token_cap_usd=payload.token_cap_usd,
        status="draft",
        current_phase=Phase.DISCOVERY,
        created_at=now,
        updated_at=now,
    )


@router.get("", response_model=list[Project])
async def list_projects(user: CurrentUser) -> list[Project]:
    # TODO: query projects WHERE owner_id = user.id
    _ = user
    return []


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: UUID, user: CurrentUser) -> Project:
    # TODO: load and check ownership.
    _ = project_id, user
    raise HTTPException(status.HTTP_404_NOT_FOUND, "not implemented")


@router.patch("/{project_id}", response_model=Project)
async def update_project(
    project_id: UUID, payload: UpdateProjectRequest, user: CurrentUser
) -> Project:
    _ = project_id, payload, user
    raise HTTPException(status.HTTP_404_NOT_FOUND, "not implemented")


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_project(project_id: UUID, user: CurrentUser) -> None:
    _ = project_id, user
    return None
