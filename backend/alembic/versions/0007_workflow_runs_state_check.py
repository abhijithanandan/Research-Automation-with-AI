"""CHECK constraint on workflow_runs.state — DB-level state-contract guard.

The application enforces the valid run-state set (schemas.VALID_RUN_STATES) at
the one write chokepoint (_update_run_state) and in startup cleanup. This
migration adds the same invariant at the DB layer so NO path — a future code
change, a manual SQL fix, a stray migration — can persist an out-of-contract
state. Motivated by the audit P0 bug where orphan cleanup wrote a non-contract
"failed" state that the column happily accepted.

Valid states (must mirror schemas.VALID_RUN_STATES / WorkflowRun.state Literal):
    running, awaiting_approval, approved, rejected, error

Uses batch_alter_table so the CHECK is added correctly on both Postgres
(ALTER TABLE ADD CONSTRAINT) and SQLite (table rebuild — SQLite cannot add a
CHECK to an existing table in place).

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT_NAME = "ck_workflow_runs_state_valid"
_VALID = "('running', 'awaiting_approval', 'approved', 'rejected', 'error')"


def upgrade() -> None:
    with op.batch_alter_table("workflow_runs") as batch:
        batch.create_check_constraint(_CONSTRAINT_NAME, f"state IN {_VALID}")


def downgrade() -> None:
    with op.batch_alter_table("workflow_runs") as batch:
        batch.drop_constraint(_CONSTRAINT_NAME, type_="check")
