"""Project CRUD routes. See SPEC.md §3.2."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.api.deps import CurrentUser, DbSession
from app.models.db import AuditLogRow, ProjectRow
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
async def create_project(
    payload: CreateProjectRequest, user: CurrentUser, db: DbSession
) -> Project:
    """Create a new project from a seed query."""
    now = datetime.now(tz=UTC)
    project_id = uuid4()

    row = ProjectRow(
        id=project_id,
        owner_id=user.id,
        title=payload.title,
        seed_query=payload.seed_query,
        output_format=payload.output_format,
        token_cap_usd=payload.token_cap_usd,
        status="draft",
        current_phase=Phase.DISCOVERY.value,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    await db.flush()  # ensure ProjectRow is persisted before audit FK reference

    db.add(
        AuditLogRow(
            id=uuid4(),
            project_id=project_id,
            workflow_run_id=None,
            actor="user",
            action="project.create",
            payload={"user_id": str(user.id), "title": payload.title},
            created_at=now,
        )
    )

    return _row_to_schema(row)


@router.get("", response_model=list[Project])
async def list_projects(user: CurrentUser, db: DbSession) -> list[Project]:
    """List all projects owned by the authenticated user."""
    rows = (
        (await db.execute(select(ProjectRow).where(ProjectRow.owner_id == user.id))).scalars().all()
    )
    return [_row_to_schema(r) for r in rows]


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: UUID, user: CurrentUser, db: DbSession) -> Project:
    row = await db.get(ProjectRow, project_id)
    _assert_owned(row, user.id, project_id)
    return _row_to_schema(row)  # type: ignore[arg-type]


@router.patch("/{project_id}", response_model=Project)
async def update_project(
    project_id: UUID,
    payload: UpdateProjectRequest,
    user: CurrentUser,
    db: DbSession,
) -> Project:
    row = await db.get(ProjectRow, project_id)
    _assert_owned(row, user.id, project_id)

    values: dict[str, object] = {"updated_at": datetime.now(tz=UTC)}
    if payload.title is not None:
        values["title"] = payload.title
    if payload.output_format is not None:
        values["output_format"] = payload.output_format
    if payload.token_cap_usd is not None:
        values["token_cap_usd"] = payload.token_cap_usd

    await db.execute(update(ProjectRow).where(ProjectRow.id == project_id).values(**values))
    # Re-fetch the updated row.
    await db.refresh(row)
    return _row_to_schema(row)  # type: ignore[arg-type]


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_project(project_id: UUID, user: CurrentUser, db: DbSession) -> None:
    """Soft-delete: set status=archived."""
    row = await db.get(ProjectRow, project_id)
    _assert_owned(row, user.id, project_id)

    await db.execute(
        update(ProjectRow)
        .where(ProjectRow.id == project_id)
        .values(status="archived", updated_at=datetime.now(tz=UTC))
    )
    db.add(
        AuditLogRow(
            id=uuid4(),
            project_id=project_id,
            workflow_run_id=None,
            actor="user",
            action="project.archive",
            payload={"user_id": str(user.id)},
            created_at=datetime.now(tz=UTC),
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert_owned(row: ProjectRow | None, user_id: UUID, project_id: UUID) -> None:
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Project {project_id} not found")
    if row.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your project")


def _row_to_schema(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        owner_id=row.owner_id,
        title=row.title,
        seed_query=row.seed_query,
        output_format=row.output_format,  # type: ignore[arg-type]
        token_cap_usd=float(row.token_cap_usd),
        status=row.status,  # type: ignore[arg-type]
        current_phase=Phase(row.current_phase),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
