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


def test_librarian_dedup_prefers_richer_source_on_collision() -> None:
    """When a DOI collision is found, prefer the source that gives us the most
    downstream value. arXiv always returns a fetchable PDF URL whereas
    Semantic Scholar often doesn't, so an arXiv entry beats a SS entry for the
    same DOI — even if SS arrived first. This is what fixes the UI's
    "everything badged Semantic Scholar" symptom: SS used to win every dedup
    race by virtue of being fastest; now richer sources win regardless of
    arrival order.
    """
    doi = "10.1234/preferred"
    first_ss = _make_paper(doi, "From SS", source="semantic_scholar")
    second_arxiv = _make_paper(doi, "From arXiv", source="arxiv")

    result = _deduplicate([first_ss, second_arxiv])

    assert len(result) == 1
    assert result[0].source == "arxiv"


def test_librarian_dedup_pdf_url_dominates_source_ranking() -> None:
    """A paper with a usable pdf_url beats one without — even if the without-
    pdf paper would otherwise be ranked higher by source. Crossref normally
    ranks lowest, but a Crossref entry that an Unpaywall enrichment populated
    a pdf_url for should beat an arXiv entry with pdf_url=None."""
    doi = "10.1234/oa-crossref"
    crossref_with_pdf = _make_paper(doi, "Same Paper", source="crossref")
    crossref_with_pdf = crossref_with_pdf.model_copy(
        update={"pdf_url": "https://oa.example.com/p.pdf"}
    )
    arxiv_no_pdf = _make_paper(doi, "Same Paper", source="arxiv")
    # Force pdf_url=None on the arxiv one to simulate the rare case.
    arxiv_no_pdf = arxiv_no_pdf.model_copy(update={"pdf_url": None})

    result = _deduplicate([arxiv_no_pdf, crossref_with_pdf])

    assert len(result) == 1
    assert result[0].source == "crossref"
    assert result[0].pdf_url is not None


def test_librarian_merges_results_from_all_five_sources() -> None:
    """All five APIs can return the same paper. Dedup must collapse them to one.

    This is the 5-source-engine invariant: when SS + arXiv + Crossref + CORE +
    Europe PMC all surface the same DOI, the final pool has a single entry.
    Distinct papers from each source coexist alongside it.
    """
    shared_doi = "10.5555/shared.2024.001"
    # Same paper appearing in all five sources.
    duplicates = [
        _make_paper(shared_doi, "A Shared Paper", source="semantic_scholar"),
        _make_paper(shared_doi, "A Shared Paper", source="arxiv"),
        _make_paper(shared_doi, "A Shared Paper", source="crossref"),
        _make_paper(shared_doi, "A Shared Paper", source="core"),
        _make_paper(shared_doi, "A Shared Paper", source="europe_pmc"),
    ]
    # Distinct papers that should survive. Titles intentionally share no
    # tokens so the fuzzy matcher does not collapse them.
    unique = [
        _make_paper(
            "10.5555/uniq.ss",
            "Transformers for Sentiment Classification",
            source="semantic_scholar",
        ),
        _make_paper(
            "arxiv:2401.99999",
            "Graph Neural Networks in Drug Discovery",
            source="arxiv",
        ),
        _make_paper(
            "10.5555/uniq.crf",
            "Bayesian Optimisation for Hyperparameter Tuning",
            source="crossref",
        ),
        _make_paper(
            "10.5555/uniq.core",
            "Reinforcement Learning Robotic Manipulation",
            source="core",
        ),
        _make_paper(
            "PMC1234567",
            "CRISPR-Cas9 Genome Editing in Mice",
            source="europe_pmc",
        ),
    ]

    result = _deduplicate(duplicates + unique)

    # 1 collapsed duplicate + 5 unique = 6 papers total.
    assert len(result) == 6
    doi_count = sum(1 for p in result if p.external_id == shared_doi)
    assert doi_count == 1, "the shared DOI must appear exactly once"
    # All five sources must be represented somewhere in the merged pool.
    sources = {p.source for p in result}
    assert sources >= {"semantic_scholar", "arxiv", "crossref", "core", "europe_pmc"}
