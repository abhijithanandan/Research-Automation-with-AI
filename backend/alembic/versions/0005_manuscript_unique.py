"""Partial unique index on artifacts(project_id) for kind='manuscript'.

Phase 4 produces exactly one assembled manuscript per project. The Scribe
agent may be re-run (after edits, after rejected sections, etc.), so the
service layer needs an atomic "replace the existing manuscript" operation
rather than "raise if one already exists". A partial unique index on
project_id WHERE kind='manuscript' makes the duplicate impossible at the
DB layer; the service uses ON CONFLICT DO NOTHING on insert and a separate
UPDATE to refresh the content when the row already exists.

Section artifacts (kind='section') and matrix/summary artifacts are
unconstrained — re-runs of those can legitimately produce multiple rows
per project (one per draft iteration) and the audit trail wants every
version preserved.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "uq_artifacts_manuscript_per_project"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "artifacts",
        ["project_id"],
        unique=True,
        postgresql_where="kind = 'manuscript'",
        sqlite_where="kind = 'manuscript'",
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="artifacts")
