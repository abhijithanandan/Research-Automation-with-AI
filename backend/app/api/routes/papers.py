"""Paper-pool routes. See SPEC.md §3.4."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.db import AuditLogRow, PaperRow, ProjectRow, WorkflowRunRow
from app.models.schemas import Paper

router = APIRouter(tags=["papers"], prefix="/projects/{project_id}/papers")


class UpdatePaperRequest(BaseModel):
    approved: bool | None = None
    citation_key: str | None = None
    title: str | None = None


@router.get("", response_model=list[Paper])
async def list_papers(project_id: UUID, user: CurrentUser, db: DbSession) -> list[Paper]:
    """List candidate + approved papers for the project."""
    await _assert_owned(db, project_id, user.id)
    rows = (
        (await db.execute(select(PaperRow).where(PaperRow.project_id == project_id)))
        .scalars()
        .all()
    )
    return [_row_to_schema(r) for r in rows]


@router.post("/upload", response_model=Paper, status_code=status.HTTP_201_CREATED)
async def upload_paper(
    project_id: UUID, file: UploadFile, user: CurrentUser, db: DbSession
) -> Paper:
    """Upload a local PDF. Metadata extraction is a Phase 2 feature (MVP: out of scope)."""
    await _assert_owned(db, project_id, user.id)
    # Stub per BRD §8 MVP out-of-scope list. Endpoint exists to satisfy SPEC §3.4.
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "PDF upload is out of scope for Phase 1 MVP.",
    )


@router.patch("/{paper_id}", response_model=Paper)
async def update_paper(
    project_id: UUID,
    paper_id: UUID,
    payload: UpdatePaperRequest,
    user: CurrentUser,
    db: DbSession,
) -> Paper:
    """Toggle approval, fix metadata, or override citation key.

    Blocked with 409 phase_locked after Phase 1 approval (SPEC §3.4).
    """
    await _assert_owned(db, project_id, user.id)
    await _assert_phase_not_locked(db, project_id)

    row = await db.get(PaperRow, paper_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")

    if payload.approved is not None:
        row.approved = payload.approved
    if payload.citation_key is not None:
        row.citation_key = payload.citation_key
    if payload.title is not None:
        row.title = payload.title

    db.add(
        AuditLogRow(
            id=uuid4(),
            project_id=project_id,
            workflow_run_id=None,
            actor="user",
            action="paper.update",
            payload={
                "user_id": str(user.id),
                "paper_id": str(paper_id),
                "approved": payload.approved,
            },
            created_at=datetime.now(tz=UTC),
        )
    )

    await db.flush()
    return _row_to_schema(row)


@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_paper(project_id: UUID, paper_id: UUID, user: CurrentUser, db: DbSession) -> None:
    """Remove a paper from the pool. Blocked after Phase 1 approval."""
    await _assert_owned(db, project_id, user.id)
    await _assert_phase_not_locked(db, project_id)

    row = await db.get(PaperRow, paper_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper not found")

    await db.delete(row)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_owned(db: DbSession, project_id: UUID, user_id: UUID) -> None:
    row = await db.get(ProjectRow, project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    if row.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your project")


async def _assert_phase_not_locked(db: DbSession, project_id: UUID) -> None:
    """Raise 409 if Phase 1 has already been approved (pool is locked)."""
    run = (
        (
            await db.execute(
                select(WorkflowRunRow)
                .where(WorkflowRunRow.project_id == project_id)
                .order_by(WorkflowRunRow.started_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )

    if run is not None and run.state in ("approved",) and run.phase == "discovery":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "phase_locked",
                "message": "Cannot modify papers after Phase 1 approval.",
            },
        )


def _row_to_schema(row: PaperRow) -> Paper:
    return Paper(
        id=row.id,
        project_id=row.project_id,
        source=row.source,  # type: ignore[arg-type]
        external_id=row.external_id,
        title=row.title,
        authors=list(row.authors),
        year=row.year,
        abstract=row.abstract,
        pdf_url=row.pdf_url,  # type: ignore[arg-type]
        citation_key=row.citation_key,
        approved=row.approved,
        added_at=row.added_at,
    )
