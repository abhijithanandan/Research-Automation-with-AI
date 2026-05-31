"""Datasets table — Phase 3 (Analyst) input storage.

User-uploaded tabular files (CSV / JSON / Parquet) are persisted out of the
hot blob path: only metadata (filename, sha256, columns, rowcount, bytes,
storage URI) lives in Postgres. The actual file bytes go to disk in dev
(file://...) and to object storage in prod (s3://...), keyed by a
per-project namespace.

Unique constraint on (project_id, sha256): re-uploading byte-identical
content for the same project is rejected with 409 by the upload route so
the audit log can never grow two "same file, different id" entries.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-31
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_uri", sa.String(), nullable=False),
        sa.Column("columns", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rowcount", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "uploaded_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "sha256", name="uq_datasets_project_sha256"),
    )
    op.create_index("ix_datasets_project", "datasets", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_datasets_project", table_name="datasets")
    op.drop_table("datasets")
