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
                "updates": payload.model_dump(exclude_none=True),
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
    """Raise 409 if the paper pool is locked.

    Authoritative locking rule (defense-in-depth, audit round-4 LOW-MED):

    Layer 1 — audit marker (authoritative):
      If the audit log contains an action="phase_1.approved_pool" entry for
      this project, the pool is **permanently** locked. This is the canonical
      record of approval written by approve_workflow at the moment the user
      confirmed the pool. A row in audit_log is append-only and survives
      state-machine resets, run reruns, and admin tinkering with run.state.

    Layer 2 — run-state heuristic (covers the gap before the audit row
    is committed and legacy runs):
      - Locked once the workflow advances past DISCOVERY (any later phase,
        regardless of run.state) — the pool is now an input to Critic / Scribe
        and must not change underneath them.
      - Locked when state == "approved" — backstop for legacy runs whose
        ``phase`` column wasn't bumped before MED-1 landed; their phase still
        reads "discovery" even after approval.
      - Locked on state == "error" — broken runs need admin intervention,
        not user edits.

    Either layer firing is sufficient to lock. The audit-marker layer exists
    so that even if some future bug corrupts run.phase/run.state, the
    immutable audit record still enforces the invariant.

    Previously the rule required *both* state == "approved" AND phase ==
    "discovery" — that combination is mutually exclusive after MED-1, which
    meant the lock never fired and users could mutate the pool after
    synthesis had already started.
    """
    # Layer 1: audit marker. One indexed query — cheap, and authoritative.
    audit_marker = (
        (
            await db.execute(
                select(AuditLogRow.id)
                .where(AuditLogRow.project_id == project_id)
                .where(AuditLogRow.action == "phase_1.approved_pool")
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if audit_marker is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "phase_locked",
                "message": "Cannot modify papers after Phase 1 approval.",
            },
        )

    # Layer 2: run-state heuristic.
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

    if run is None:
        return
    locked = run.phase != "discovery" or run.state == "approved" or run.state == "error"
    if locked:
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
        citation_count=row.citation_count,
        approved=row.approved,
        added_at=row.added_at,
    )
