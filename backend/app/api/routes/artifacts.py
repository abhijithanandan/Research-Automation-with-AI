"""Artifact retrieval and export routes. See SPEC.md §3.5."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.db import ArtifactRow, AuditLogRow, ProjectRow
from app.models.schemas import Artifact, AuditLogEntry

router = APIRouter(tags=["artifacts"])


@router.get(
    "/projects/{project_id}/artifacts",
    response_model=list[Artifact],
)
async def list_artifacts(
    project_id: UUID,
    user: CurrentUser,
    db: DbSession,
    kind: str | None = Query(default=None),
) -> list[Artifact]:
    """List artifacts for a project, optionally filtered by kind."""
    await _assert_owned(db, project_id, user.id)

    q = select(ArtifactRow).where(ArtifactRow.project_id == project_id)
    if kind is not None:
        q = q.where(ArtifactRow.kind == kind)

    rows = (await db.execute(q)).scalars().all()
    return [_artifact_to_schema(r) for r in rows]


@router.get(
    "/projects/{project_id}/artifacts/{artifact_id}",
    response_model=Artifact,
)
async def get_artifact(
    project_id: UUID, artifact_id: UUID, user: CurrentUser, db: DbSession
) -> Artifact:
    await _assert_owned(db, project_id, user.id)
    row = await db.get(ArtifactRow, artifact_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")
    return _artifact_to_schema(row)


@router.get("/projects/{project_id}/export")
async def export_manuscript(
    project_id: UUID,
    user: CurrentUser,
    db: DbSession,
    format: str = Query(default="markdown", pattern="^(markdown|latex|bibtex)$"),
) -> dict[str, str]:
    """Export the manuscript. Phase 4 feature; returns 501 until Scribe is wired."""
    await _assert_owned(db, project_id, user.id)
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "Export is available after Phase 4 (Scribe) is complete.",
    )


@router.get("/projects/{project_id}/audit", response_model=list[AuditLogEntry])
async def get_audit_log(
    project_id: UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[AuditLogEntry]:
    """Paginated audit log, newest first."""
    await _assert_owned(db, project_id, user.id)
    rows = (
        (
            await db.execute(
                select(AuditLogRow)
                .where(AuditLogRow.project_id == project_id)
                .order_by(AuditLogRow.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [_audit_to_schema(r) for r in rows]


@router.get("/projects/{project_id}/usage")
async def get_usage(project_id: UUID, user: CurrentUser, db: DbSession) -> dict[str, object]:
    """Token + cost rollup for a project."""
    await _assert_owned(db, project_id, user.id)
    from sqlalchemy import func

    row = (
        await db.execute(
            select(
                func.coalesce(func.sum(AuditLogRow.tokens_in), 0).label("tokens_in"),
                func.coalesce(func.sum(AuditLogRow.tokens_out), 0).label("tokens_out"),
                func.coalesce(func.sum(AuditLogRow.cost_usd), 0.0).label("cost_usd"),
            ).where(AuditLogRow.project_id == project_id)
        )
    ).one()

    return {
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "cost_usd": float(row.cost_usd),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_owned(db: DbSession, project_id: UUID, user_id: UUID) -> None:
    row = await db.get(ProjectRow, project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    if row.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your project")


def _artifact_to_schema(row: ArtifactRow) -> Artifact:

    return Artifact(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,  # type: ignore[arg-type]
        label=row.label,
        content=row.content,
        mime_type=row.mime_type,
        produced_by=row.produced_by,  # type: ignore[arg-type]
        parent_id=row.parent_id,
        created_at=row.created_at,
    )


def _audit_to_schema(row: AuditLogRow) -> AuditLogEntry:
    return AuditLogEntry(
        id=row.id,
        project_id=row.project_id,
        workflow_run_id=row.workflow_run_id,
        actor=row.actor,  # type: ignore[arg-type]
        action=row.action,
        payload=dict(row.payload),
        model=row.model,
        tokens_in=row.tokens_in,
        tokens_out=row.tokens_out,
        cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
        created_at=row.created_at,
    )
