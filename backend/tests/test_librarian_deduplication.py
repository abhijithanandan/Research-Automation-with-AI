"""Tests for the Librarian's deduplication logic.

Required by docs/agents/librarian.md §Tests required:
  - test_librarian_dedupes_by_doi
  - test_librarian_dedupes_by_fuzzy_title
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.agents.librarian import _deduplicate
from app.models.schemas import Paper


def _make_paper(
    external_id: str,
    title: str,
    source: str = "arxiv",
) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=uuid4(),
        source=source,  # type: ignore[arg-type]
        external_id=external_id,
        title=title,
        authors=["Smith, J"],
        year=2023,
        abstract=None,
        pdf_url=None,
        citation_key="",
        approved=False,
        added_at=datetime.now(tz=UTC),
    )


def test_librarian_dedupes_by_doi() -> None:
    """Two papers with the same DOI should collapse to one."""
    doi = "10.1234/example.2023"
    p1 = _make_paper(doi, "Paper Alpha", source="semantic_scholar")
    p2 = _make_paper(doi, "Paper Alpha (duplicate)", source="arxiv")

    result = _deduplicate([p1, p2])

    assert len(result) == 1
    assert result[0].external_id == doi


def test_librarian_dedupes_by_fuzzy_title() -> None:
    """Two papers without a DOI but with very similar titles should dedup."""
    # These titles differ only by a single word — token_set_ratio will be ≥ 90.
    p1 = _make_paper("arxiv:2301.00001", "Attention Is All You Need")
    p2 = _make_paper("arxiv:2301.00002", "Attention Is All You Need!")

    result = _deduplicate([p1, p2])

    assert len(result) == 1


def test_librarian_keeps_distinct_titles() -> None:
    """Papers with clearly different titles must both survive dedup."""
    p1 = _make_paper("arxiv:2301.00001", "Transformers for NLP")
    p2 = _make_paper("arxiv:2301.00002", "Convolutional Neural Networks for Vision")

    result = _deduplicate([p1, p2])

    assert len(result) == 2


def test_librarian_dedup_prefers_first_occurrence() -> None:
    """When a DOI collision is found, the first paper is kept."""
    doi = "10.1234/preferred"
    first = _make_paper(doi, "First Paper", source="semantic_scholar")
    second = _make_paper(doi, "Second Paper (dup)", source="arxiv")

    result = _deduplicate([first, second])

    assert result[0].title == "First Paper"
