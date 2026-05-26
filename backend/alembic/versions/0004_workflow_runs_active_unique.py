"""Partial unique index on workflow_runs(project_id) for active states.

Closes the read-then-insert race in services.workflow.start_workflow where
two concurrent invocations on the same project_id could both find no
"existing" active run, both insert, and both reach the graph dispatcher —
producing duplicate active runs and ambiguous WebSocket state.

The index is *partial*: it only enforces uniqueness when state is in the
active set (running / awaiting_approval). Historical runs (approved, error,
cancelled) are unconstrained and can accumulate as many as needed.

The service-layer fallback (try/except IntegrityError → re-select existing
→ return it) makes the race lose gracefully: the loser of the insert race
gets the winner's row, exactly the same shape callers see today when an
active run already exists.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "uq_workflow_runs_active_project"


def upgrade() -> None:
    # Postgres supports the WHERE clause natively; SQLite supports it too
    # (since 3.8), so the same DDL works against both backends.
    op.create_index(
        _INDEX_NAME,
        "workflow_runs",
        ["project_id"],
        unique=True,
        postgresql_where="state IN ('running', 'awaiting_approval')",
        sqlite_where="state IN ('running', 'awaiting_approval')",
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="workflow_runs")
