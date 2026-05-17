"""Tests for citation-key generation and max_candidates trimming.

Required by docs/agents/librarian.md §Tests required:
  - test_librarian_generates_unique_citation_keys
  - test_librarian_returns_at_most_max_candidates
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.models.schemas import Paper
from app.services.discovery import generate_citation_keys


def _paper(title: str, author: str = "Smith, Jane", year: int = 2020) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=uuid4(),
        source="arxiv",  # type: ignore[arg-type]
        external_id=f"arxiv:{uuid4().hex[:8]}",
        title=title,
        authors=[author],
        year=year,
        citation_key="",
        approved=False,
        added_at=datetime.now(tz=UTC),
    )


def test_librarian_generates_unique_citation_keys() -> None:
    """Citation keys must be unique even when two papers share author + year."""
    papers = [
        _paper("Paper A", author="Smith, John", year=2020),
        _paper("Paper B", author="Smith, John", year=2020),
        _paper("Paper C", author="Smith, John", year=2020),
    ]

    keyed = generate_citation_keys(papers)
    keys = [p.citation_key for p in keyed]

    assert len(set(keys)) == 3, f"Expected 3 unique keys, got {keys}"
    # First should be smith2020, second smith2020a, third smith2020b
    assert "smith2020" in keys
    assert "smith2020a" in keys
    assert "smith2020b" in keys


def test_librarian_key_format() -> None:
    """Citation key should be lowercase-last-name + year."""
    papers = [_paper("My Paper", author="O'Brien, Alice", year=2022)]
    keyed = generate_citation_keys(papers)
    # Non-alphanumeric stripped: "obrien" + "2022"
    assert keyed[0].citation_key == "obrien2022"


def test_librarian_returns_at_most_max_candidates() -> None:
    """After dedup + trim, result must not exceed max_candidates."""
    max_c = 5
    papers = [_paper(f"Paper {i}", year=2020 + i) for i in range(20)]

    trimmed = papers[:max_c]
    keyed = generate_citation_keys(trimmed)

    assert len(keyed) <= max_c


def test_citation_key_no_author() -> None:
    """If no authors, fall back to 'unknown' prefix."""
    paper = Paper(
        id=uuid4(),
        project_id=uuid4(),
        source="arxiv",  # type: ignore[arg-type]
        external_id="no-author",
        title="Authorless Paper",
        authors=[],
        year=2021,
        citation_key="",
        approved=False,
        added_at=datetime.now(tz=UTC),
    )
    keyed = generate_citation_keys([paper])
    assert keyed[0].citation_key == "unknown2021"
