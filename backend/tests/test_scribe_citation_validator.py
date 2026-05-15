"""The citation-validator invariant from SPEC.md §6.4."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.agents.scribe import Scribe
from app.models.schemas import Paper


def _paper(key: str) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=uuid4(),
        source="semantic_scholar",
        external_id=f"ext-{key}",
        title=f"Paper {key}",
        authors=["Anon"],
        citation_key=key,
        added_at=datetime.now(tz=UTC),
    )


def test_all_cited_keys_in_pool_returns_empty_set() -> None:
    pool = [_paper("smith2020"), _paper("jones2021")]
    unknown = Scribe.validate_citations(["smith2020", "jones2021"], pool)
    assert unknown == set()


def test_unknown_citation_is_flagged() -> None:
    pool = [_paper("smith2020")]
    unknown = Scribe.validate_citations(["smith2020", "ghost1999"], pool)
    assert unknown == {"ghost1999"}
