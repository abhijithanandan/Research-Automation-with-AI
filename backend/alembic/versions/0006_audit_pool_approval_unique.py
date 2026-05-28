"""Partial unique index on audit_log(workflow_run_id) for phase_1.approved_pool.

The paper-pool lock (round-4 LOW-MED, defence-in-depth in
``app/api/routes/papers.py``) is keyed on the existence of an
``action='phase_1.approved_pool'`` audit row for the project. If a bug or
a concurrent approve double-fires the audit insert, the lock still works
(any row turns it on) but downstream consumers that count the rows would
see two and misbehave. The partial unique index forbids the duplicate at
the DB layer, regardless of which code path tried to write it.

Why partial: the audit_log table holds every action ever taken on a
project — most of which are repeatable (``user.approve`` fires once per
gate per run; ``phase_4.section_ready`` fires seven times per run). Only
the phase_1 approval marker should be unique-per-run, hence the
``WHERE action = 'phase_1.approved_pool'`` clause. Postgres and SQLite
both support partial unique indexes natively.

We also include ``workflow_run_id IS NOT NULL`` so the (extremely unusual)
case of an audit row with a NULL workflow_run_id doesn't collide.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "uq_audit_pool_approval_per_run"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "audit_log",
        ["workflow_run_id"],
        unique=True,
        postgresql_where=("action = 'phase_1.approved_pool' AND workflow_run_id IS NOT NULL"),
        sqlite_where=("action = 'phase_1.approved_pool' AND workflow_run_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="audit_log")
