"""SQLAlchemy ORM models. Schema mirrors SPEC.md §2.3."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_TS = TIMESTAMP(timezone=True)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    firebase_uid: Mapped[str] = mapped_column(String, unique=True)
    email: Mapped[str] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TS)


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
    )
    title: Mapped[str] = mapped_column(String)
    seed_query: Mapped[str] = mapped_column(Text)
    output_format: Mapped[str] = mapped_column(String, default="markdown")
    token_cap_usd: Mapped[float] = mapped_column(Numeric(10, 2), default=5.0)
    status: Mapped[str] = mapped_column(String, default="draft")
    current_phase: Mapped[str] = mapped_column(String, default="discovery")
    created_at: Mapped[datetime] = mapped_column(_TS)
    updated_at: Mapped[datetime] = mapped_column(_TS)


class WorkflowRunRow(Base):
    __tablename__ = "workflow_runs"
    # Partial unique index: at most one active run per project. The
    # read-then-insert pattern in services.workflow.start_workflow is
    # not atomic; two concurrent invocations could both find no active
    # run and both insert. The DB index makes the second insert raise
    # IntegrityError, which the service catches and resolves by returning
    # the winner's row.
    __table_args__ = (
        Index(
            "uq_workflow_runs_active_project",
            "project_id",
            unique=True,
            # SQLAlchemy 2.x rejects raw strings for index WHERE clauses
            # (the SQLite dialect compiles the expression directly). text()
            # wraps a literal SQL fragment that both dialects accept.
            postgresql_where=text("state IN ('running', 'awaiting_approval')"),
            sqlite_where=text("state IN ('running', 'awaiting_approval')"),
        ),
        # DB-level state-contract guard (mirrors alembic 0007 + VALID_RUN_STATES).
        # Present on the ORM so test DBs built via create_all() enforce it too.
        CheckConstraint(
            "state IN ('running', 'awaiting_approval', 'approved', 'rejected', 'error')",
            name="ck_workflow_runs_state_valid",
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    phase: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    checkpoint_id: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(_TS)
    awaiting_since: Mapped[datetime | None] = mapped_column(_TS, nullable=True)
    last_event_at: Mapped[datetime] = mapped_column(_TS)


class PaperRow(Base):
    __tablename__ = "papers"
    # Uniqueness on (project_id, citation_key) prevents the check-then-insert
    # race in `_persist_candidates`: two concurrent discovery runs for the same
    # project (e.g. retry after a transient failure) used to be able to create
    # duplicate paper rows because the existence check and insert were not
    # atomic. The constraint here makes the DB the source of truth; the
    # service layer uses INSERT ... ON CONFLICT DO NOTHING so the race is
    # resolved without an application-level error.
    __table_args__ = (
        UniqueConstraint("project_id", "citation_key", name="uq_papers_project_citation_key"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    source: Mapped[str] = mapped_column(String)
    external_id: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(Text)
    authors: Mapped[list[str]] = mapped_column(JSON)
    year: Mapped[int | None]
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_url: Mapped[str | None] = mapped_column(String, nullable=True)
    citation_key: Mapped[str] = mapped_column(String)
    citation_count: Mapped[int | None]
    approved: Mapped[bool] = mapped_column(default=False)
    added_at: Mapped[datetime] = mapped_column(_TS)


class DatasetRow(Base):
    """User-uploaded tabular datasets for the Analyst (Phase 3, FR-2.3).

    Unique on (project_id, sha256): a duplicate upload is rejected with 409.
    `storage_uri` is a `file://...` URI in dev (under DATA_DIR), `s3://...`
    in prod. Adapter logic in `services.dataset_storage`.
    """

    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("project_id", "sha256", name="uq_datasets_project_sha256"),
        Index("ix_datasets_project", "project_id"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    filename: Mapped[str] = mapped_column(String)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_uri: Mapped[str] = mapped_column(String)
    columns: Mapped[list[str]] = mapped_column(JSON)
    rowcount: Mapped[int] = mapped_column(Integer)
    bytes: Mapped[int] = mapped_column(BigInteger)
    uploaded_at: Mapped[datetime] = mapped_column(_TS)


class Bm25IndexRow(Base):
    """Persisted BM25 sparse-retrieval corpus for the Critic's hybrid search.

    One row per Chroma namespace (the namespace is ``str(project_id)``), so a
    project's keyword index survives backend restarts and stays in sync with
    the dense ChromaDB collection. We store the *raw* chunk texts (not a
    pickled BM25 object): the in-memory ``BM25Okapi`` is rebuilt from tokens on
    load (cheap), which keeps the column dialect-portable (JSON on sqlite in
    tests, JSONB on Postgres) and avoids a `pickle.load` bandit finding.

    `namespace` is a plain string PK with no FK to ``projects``: Chroma treats
    namespaces as opaque strings (tests use ``"project-abc"``), and the row is
    cleaned up explicitly when a project's vector data is cleared rather than
    via cascade.

    `corpus` shape: ``{"doc_ids": list[str], "doc_texts": list[str]}`` — the
    two lists are positionally aligned.
    """

    __tablename__ = "bm25_index"

    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    corpus: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(_TS)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    # Partial unique index: at most one manuscript per project. Section/
    # matrix/summary artifacts are unconstrained — re-runs legitimately
    # produce one row per iteration. Mirrors alembic 0005.
    __table_args__ = (
        Index(
            "uq_artifacts_manuscript_per_project",
            "project_id",
            unique=True,
            postgresql_where=text("kind = 'manuscript'"),
            sqlite_where=text("kind = 'manuscript'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String)
    produced_by: Mapped[str] = mapped_column(String)
    parent_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("artifacts.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(_TS)


class AuditLogRow(Base):
    __tablename__ = "audit_log"
    # M2-A: at most one phase_1.approved_pool audit row per workflow run.
    # Partial unique — every other action (user.approve fires multiple
    # times, section_ready fires seven times per run, etc.) is
    # unconstrained.
    __table_args__ = (
        Index(
            "uq_audit_pool_approval_per_run",
            "workflow_run_id",
            unique=True,
            postgresql_where=text(
                "action = 'phase_1.approved_pool' AND workflow_run_id IS NOT NULL"
            ),
            sqlite_where=text("action = 'phase_1.approved_pool' AND workflow_run_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    workflow_run_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("workflow_runs.id"), nullable=True
    )
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    tokens_in: Mapped[int | None]
    tokens_out: Mapped[int | None]
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TS)
