"""Tighten projects.owner_id FK with ondelete=RESTRICT.

The initial migration created the constraint with the default ON DELETE NO
ACTION. That's effectively the same as RESTRICT for immediate constraints,
but it relies on Postgres's default rather than declaring intent. Coderabbit
review (PR #5, 2026-05-26) flagged that missing ondelete is a footgun: a
future operator running `DELETE FROM users WHERE ...` should either be told
"no" (RESTRICT) or be aware that everything cascades. We pick RESTRICT —
cascading would silently wipe a user's research history; refusing the delete
makes the intent loud.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("projects_owner_id_fkey", "projects", type_="foreignkey")
    op.create_foreign_key(
        "projects_owner_id_fkey",
        "projects",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("projects_owner_id_fkey", "projects", type_="foreignkey")
    op.create_foreign_key(
        "projects_owner_id_fkey",
        "projects",
        "users",
        ["owner_id"],
        ["id"],
    )
