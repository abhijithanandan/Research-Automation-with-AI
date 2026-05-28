"""Tests for the Phase 4 manuscript assembly (B-4).

`node_assemble` concatenates the approved section drafts in canonical order
(abstract → introduction → related_work → methodology → results → discussion
→ conclusion) into a single manuscript artifact. The service layer persists
that artifact to the `artifacts` table with kind="manuscript" — at most one
per project, enforced by the partial unique index in alembic 0005.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.graph.workflow import node_assemble
from app.models.schemas import Phase

TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000050")

CANONICAL_ORDER = [
    "abstract",
    "introduction",
    "related_work",
    "methodology",
    "results",
    "discussion",
    "conclusion",
]


def _draft(section: str, content: str | None = None) -> dict[str, object]:
    """Construct a `drafts` list entry matching the Scribe wire shape."""
    now = datetime.now(tz=UTC)
    return {
        "section": section,
        "artifact": {
            "id": str(uuid4()),
            "project_id": str(TEST_PROJECT_ID),
            "kind": "section",
            "label": section,
            "content": content or f"## {section.title()}\n\nBody of {section}.",
            "mime_type": "text/markdown",
            "produced_by": "scribe",
            "parent_id": None,
            "created_at": now.isoformat(),
        },
        "cited_keys": [],
    }


def _state(drafts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "project_id": TEST_PROJECT_ID,
        "workflow_run_id": uuid4(),
        "phase": Phase.DRAFTING,
        "drafts": drafts,
        "sections_done": [d["section"] for d in drafts],
        "sections_remaining": [],
    }


# ---------------------------------------------------------------------------
# B-8 required assemble tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assemble_concatenates_sections_in_canonical_order() -> None:
    """Drafts may arrive in any order; the assembled manuscript must reorder
    them to the canonical BRD §5.2 sequence."""
    # Deliberately scrambled order.
    drafts = [
        _draft("conclusion"),
        _draft("abstract"),
        _draft("results"),
        _draft("methodology"),
        _draft("discussion"),
        _draft("introduction"),
        _draft("related_work"),
    ]
    out = await node_assemble(_state(drafts))  # type: ignore[arg-type]

    manuscript = out.get("manuscript")
    assert manuscript is not None
    assert manuscript["kind"] == "manuscript"
    assert manuscript["mime_type"] == "text/markdown"

    content = manuscript["content"]
    # Each section header appears once, in the canonical order.
    positions = [content.find(f"Body of {s}") for s in CANONICAL_ORDER]
    assert all(p >= 0 for p in positions), "every section must appear in the manuscript"
    assert positions == sorted(positions), "sections must be in canonical order"


@pytest.mark.asyncio
async def test_assemble_writes_title_page_header() -> None:
    """The assembled manuscript must begin with a title-page block (project
    title placeholder + ResearchFlow attribution + ISO date)."""
    drafts = [_draft(s) for s in CANONICAL_ORDER]
    out = await node_assemble(_state(drafts))  # type: ignore[arg-type]

    content = out["manuscript"]["content"]
    # Title page must come before the first section body.
    title_idx = content.find("ResearchFlow AI")
    abstract_idx = content.find("Body of abstract")
    assert 0 <= title_idx < abstract_idx, "title page must precede the body sections"
    # Horizontal rule separates the title page from the body.
    assert "---" in content[:abstract_idx]


@pytest.mark.asyncio
async def test_assemble_skips_missing_sections_gracefully() -> None:
    """If a section is missing from drafts (defence-in-depth, should never
    happen at runtime), assemble must still produce a manuscript containing
    only the sections that *are* present, in canonical order."""
    drafts = [_draft("abstract"), _draft("conclusion")]
    out = await node_assemble(_state(drafts))  # type: ignore[arg-type]

    content = out["manuscript"]["content"]
    assert "Body of abstract" in content
    assert "Body of conclusion" in content
    # Missing sections silently absent (not crash, not empty placeholder).
    assert "Body of methodology" not in content
