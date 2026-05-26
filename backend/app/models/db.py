"""SQLAlchemy ORM models. Schema mirrors SPEC.md §2.3."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, TIMESTAMP, ForeignKey, Numeric, String, Text, UniqueConstraint
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


class ArtifactRow(Base):
    __tablename__ = "artifacts"

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
