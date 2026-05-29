"""Artifact retrieval and export routes. See SPEC.md §3.5."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.db import ArtifactRow, AuditLogRow, ProjectRow
from app.models.schemas import Artifact, AuditLogEntry
from app.services import export as export_svc
from app.services.citations import SectionCitationPanel

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
    # Only the v0.1 formats are accepted. LaTeX is NOT listed — a `latex`
    # value gets the ordinary 422 validation error (no special messaging).
    format: str = Query(default="markdown", pattern="^(markdown|bibtex|package|bundle)$"),
) -> Response:
    """Export the manuscript (BRD FR-3.5). Requires an assembled manuscript
    (Phase 4 done); otherwise 409 manuscript_not_ready.

    - markdown : the manuscript text (text/markdown).
    - bibtex   : approved-pool references (application/x-bibtex).
    - package  : ZIP of separate files (manuscript/references/disclosure/audit).
    - bundle   : one combined markdown file (text/markdown).
    """
    project = await _assert_owned(db, project_id, user.id)

    try:
        if format == "markdown":
            content = await export_svc.build_manuscript_markdown(db, project_id)
            resp: Response = Response(content=content, media_type="text/markdown")
            filename = "manuscript.md"
        elif format == "bibtex":
            content = await export_svc.build_bibtex(db, project_id)
            resp = Response(content=content, media_type="application/x-bibtex")
            filename = "references.bib"
        elif format == "bundle":
            content = await export_svc.build_bundle_markdown(db, project_id)
            resp = Response(content=content, media_type="text/markdown")
            filename = "manuscript-bundle.md"
        else:  # package
            data = await export_svc.build_package_zip(db, project_id, project.title)
            resp = Response(content=data, media_type="application/zip")
            filename = "manuscript-package.zip"
    except export_svc.ManuscriptNotReadyError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "manuscript_not_ready", "message": str(exc)},
        ) from exc

    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'

    # Audit the export (BRD §4.3 — every consequential action is recorded).
    db.add(
        AuditLogRow(
            id=uuid4(),
            project_id=project_id,
            workflow_run_id=None,
            actor="user",
            action="export.generated",
            payload={"format": format, "user_id": str(user.id)},
            created_at=datetime.now(tz=UTC),
        )
    )
    await db.flush()
    return resp


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


@router.get("/projects/{project_id}/drafting/citations")
async def get_section_citations(
    project_id: UUID,
    user: CurrentUser,
    db: DbSession,
    section: str = Query(..., min_length=1, max_length=64),
) -> SectionCitationPanel:
    """Citation Manager v1 (BRD FR-1.5) — for the latest draft of `section`,
    return the cited keys, the unresolved (offending) keys, and resolved
    paper metadata for one-click inspection in the review panel."""
    from app.services.citations import resolve_section_citations

    await _assert_owned(db, project_id, user.id)
    return await resolve_section_citations(db, project_id, section)


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

    # Additive Phase-4 telemetry block (NFR-6 / BRD §9). Existing consumers
    # that only read tokens/cost keep working — this is a new sibling key.
    from app.services.workflow import drafting_telemetry

    return {
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "cost_usd": float(row.cost_usd),
        "drafting": await drafting_telemetry(db, project_id),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_owned(db: DbSession, project_id: UUID, user_id: UUID) -> ProjectRow:
    """Verify ownership and return the project row (callers may use it; the
    others ignore the return — non-breaking)."""
    row = await db.get(ProjectRow, project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    if row.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your project")
    return row


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
