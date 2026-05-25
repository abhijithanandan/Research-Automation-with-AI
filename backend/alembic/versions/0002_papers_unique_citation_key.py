"""Add unique constraint on papers(project_id, citation_key).

Closes the check-then-insert race in services.workflow._persist_candidates
(audit round-3, CRIT-2). Concurrent discovery retries used to be able to
create duplicate paper rows because the existence check and the insert were
not atomic; the constraint here makes the DB the source of truth and lets
the service layer use INSERT ... ON CONFLICT DO NOTHING.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-24
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # If the DB already contains duplicates from before this migration ran,
    # the constraint creation will fail. Operators should run a cleanup query
    # first (see backend/docs/audit/phase-2-audit.md). For fresh installs
    # this is a no-op.
    op.create_unique_constraint(
        "uq_papers_project_citation_key",
        "papers",
        ["project_id", "citation_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_papers_project_citation_key", "papers", type_="unique")
