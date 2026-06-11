"""BM25 sparse-retrieval corpus — Phase 2 hybrid search.

Persists the Critic's per-namespace keyword index so it survives backend
restarts and stays aligned with the dense ChromaDB collection. One row per
Chroma namespace (``str(project_id)``); the ``corpus`` JSONB holds the raw
chunk texts + ids (``{"doc_ids": [...], "doc_texts": [...]}``) and the
in-memory ``BM25Okapi`` is rebuilt from them on load.

No FK to ``projects``: Chroma namespaces are opaque strings. Rows are removed
explicitly when a project's vector data is cleared.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bm25_index",
        sa.Column("namespace", sa.String(), nullable=False),
        sa.Column("corpus", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("namespace"),
    )


def downgrade() -> None:
    op.drop_table("bm25_index")
