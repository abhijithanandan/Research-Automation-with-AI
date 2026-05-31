"""Dataset routes — Phase 3 (Analyst) input pipeline.

SPEC v0.3 §3.7. Endpoints:

    POST   /projects/{id}/datasets/upload   — multipart, returns Dataset
    GET    /projects/{id}/datasets           — list
    DELETE /projects/{id}/datasets/{ds_id}   — remove (locked once Phase 3 starts)

The route layer is thin: validation + ownership + audit. All storage logic
lives in :mod:`app.services.dataset_storage`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession
from app.api.rate_limit import rate_limit
from app.config import get_settings
from app.models.db import AuditLogRow, DatasetRow, ProjectRow, WorkflowRunRow
from app.models.schemas import Dataset
from app.services import dataset_storage
from app.utils.logging import get_logger

router = APIRouter(tags=["datasets"], prefix="/projects/{project_id}/datasets")
_log = get_logger(__name__)


@router.get("", response_model=list[Dataset])
async def list_datasets(project_id: UUID, user: CurrentUser, db: DbSession) -> list[Dataset]:
    """List the project's uploaded datasets (newest first)."""
    await _assert_owned(db, project_id, user.id)
    rows = (
        (
            await db.execute(
                select(DatasetRow)
                .where(DatasetRow.project_id == project_id)
                .order_by(DatasetRow.uploaded_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_row_to_schema(r) for r in rows]


@router.post(
    "/upload",
    response_model=Dataset,
    status_code=status.HTTP_201_CREATED,
    # Uploads are heavier than the average write; 10/min/user is conservative.
    dependencies=[Depends(rate_limit("dataset.upload", max_per_window=10))],
)
async def upload_dataset(
    project_id: UUID,
    user: CurrentUser,
    db: DbSession,
    file: Annotated[UploadFile, File(...)],
    label: Annotated[str | None, Form()] = None,
) -> Dataset:
    """Multipart upload. Stores the bytes on disk and writes a DatasetRow.

    Errors:
      - 404 if the project doesn't exist or isn't owned by the caller
      - 409 if the project is past Phase 2 approval (datasets are an input
        to Phase 3 — they must be loaded *before* analysis starts)
      - 409 if a byte-identical file already exists for this project
      - 413 if the upload exceeds ``settings.max_dataset_bytes``
      - 422 if the file's extension is unsupported or the contents are
        unparseable
    """
    _ = label  # reserved for future user-supplied display label
    await _assert_owned(db, project_id, user.id)
    await _assert_pre_analysis(db, project_id)

    if file.filename is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "filename required")

    raw = await file.read()
    settings = get_settings()
    if len(raw) > settings.max_dataset_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Dataset too large ({len(raw)} > {settings.max_dataset_bytes} bytes).",
        )

    dataset_id = uuid4()
    try:
        stored = dataset_storage.store(project_id, dataset_id, file.filename, raw)
    except dataset_storage.DatasetTooLarge as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    except dataset_storage.DatasetParseError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    now = datetime.now(tz=UTC)
    row = DatasetRow(
        id=dataset_id,
        project_id=project_id,
        filename=file.filename,
        sha256=stored.sha256,
        storage_uri=stored.storage_uri,
        columns=stored.columns,
        rowcount=stored.rowcount,
        bytes=stored.bytes,
        uploaded_at=now,
    )
    db.add(row)
    db.add(
        AuditLogRow(
            id=uuid4(),
            project_id=project_id,
            workflow_run_id=None,
            actor="user",
            action="dataset.upload",
            payload={
                "user_id": str(user.id),
                "dataset_id": str(dataset_id),
                "filename": file.filename,
                "sha256": stored.sha256,
                "rowcount": stored.rowcount,
                "columns_n": len(stored.columns),
                "bytes": stored.bytes,
            },
            created_at=now,
        )
    )

    try:
        await db.flush()
    except IntegrityError as exc:
        # Unique (project_id, sha256) collision — same bytes uploaded twice.
        # Roll back the just-created file so the FS doesn't leak orphans.
        await db.rollback()
        dataset_storage.delete(stored.storage_uri)
        _log.info(
            "dataset.upload.duplicate",
            project_id=str(project_id),
            user_id=str(user.id),
            sha256=stored.sha256,
        )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "dataset_duplicate",
                "message": "A byte-identical dataset is already uploaded for this project.",
            },
        ) from exc

    return _row_to_schema(row)


@router.delete(
    "/{dataset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("dataset.delete", max_per_window=20))],
)
async def delete_dataset(
    project_id: UUID,
    dataset_id: UUID,
    user: CurrentUser,
    db: DbSession,
) -> None:
    """Remove a dataset. Locked once Phase 3 has consumed it."""
    await _assert_owned(db, project_id, user.id)
    await _assert_pre_analysis(db, project_id)

    row = await db.get(DatasetRow, dataset_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dataset not found")

    storage_uri = row.storage_uri
    await db.delete(row)
    db.add(
        AuditLogRow(
            id=uuid4(),
            project_id=project_id,
            workflow_run_id=None,
            actor="user",
            action="dataset.delete",
            payload={
                "user_id": str(user.id),
                "dataset_id": str(dataset_id),
            },
            created_at=datetime.now(tz=UTC),
        )
    )
    await db.flush()
    # File deletion is best-effort and not rolled back if the transaction
    # later fails — orphaned bytes on disk are recoverable; a partial DB
    # commit is not.
    dataset_storage.delete(storage_uri)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _assert_owned(db: DbSession, project_id: UUID, user_id: UUID) -> None:
    row = await db.get(ProjectRow, project_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    if row.owner_id != user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not your project")


async def _assert_pre_analysis(db: DbSession, project_id: UUID) -> None:
    """409 if the project has advanced into (or past) Phase 3.

    Once Phase 3's `analyze_propose` node has run, deleting the dataset out
    from under it would invalidate the generated code's references to the
    columns. The rule is: datasets are mutable only while the project is in
    Phase 1 (discovery) or Phase 2 (synthesis), and never after Phase 4
    (drafting) starts using analyst-produced figures.
    """
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
    if run.phase in ("analysis", "drafting", "done"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "phase_locked",
                "message": "Datasets are locked once analysis has started.",
            },
        )


def _row_to_schema(row: DatasetRow) -> Dataset:
    return Dataset(
        id=row.id,
        project_id=row.project_id,
        filename=row.filename,
        sha256=row.sha256,
        columns=list(row.columns),
        rowcount=row.rowcount,
        bytes=row.bytes,
        uploaded_at=row.uploaded_at,
    )
