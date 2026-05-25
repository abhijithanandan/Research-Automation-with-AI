"""Tests for the FullTextFetcher service (BRD FR-1.2).

Covers the safety-critical bits:
  - Only allow-listed OA hosts are fetched. Google Scholar, IEEE, ResearchGate
    URLs must be skipped — the BRD does not authorise hitting those.
  - Non-PDF responses are detected and dropped (no HTML interstitials embedded).
  - Long paragraphs are split under the chunk-size cap.
  - The fetcher tolerates a missing PDF URL gracefully.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from app.models.schemas import Paper
from app.services.fulltext_fetcher import FullTextFetcher

TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000011")


def _paper(citation_key: str, pdf_url: str | None) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=TEST_PROJECT_ID,
        source="arxiv",
        external_id=f"arxiv:{citation_key}",
        title=f"Title for {citation_key}",
        authors=["Smith, J"],
        year=2024,
        abstract="An abstract.",
        pdf_url=pdf_url,  # type: ignore[arg-type]
        citation_key=citation_key,
        approved=True,
        added_at=datetime.now(tz=UTC),
    )


class _FakeVectorStore:
    """Records every upsert so tests can assert on what was embedded."""

    def __init__(self) -> None:
        self.upserts: list[tuple[str, list[dict[str, object]]]] = []

    async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None:
        self.upserts.append((namespace, documents))

    async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]:
        return []


def test_resolve_pdf_url_accepts_known_oa_hosts() -> None:
    """arXiv, Semantic Scholar, and listed OA mirrors are allow-listed."""
    vs = _FakeVectorStore()
    f = FullTextFetcher(vector_store=vs)

    for url in [
        "https://arxiv.org/pdf/2401.00001",
        "https://www.semanticscholar.org/paper/abc.pdf",
        "https://aclanthology.org/2020.acl-main.447.pdf",
        "https://www.biorxiv.org/content/10.1101/2020.01.01.000001v1.full.pdf",
        "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12345/pdf/foo.pdf",
    ]:
        p = _paper("x2024", url)
        assert f._resolve_pdf_url(p) == url, f"expected to accept {url}"


def test_resolve_pdf_url_rejects_disallowed_hosts() -> None:
    """Sites whose ToS forbid automated extraction must be skipped — the
    fetcher allow-lists; it never reaches out to Google Scholar / IEEE / RG."""
    vs = _FakeVectorStore()
    f = FullTextFetcher(vector_store=vs)

    for url in [
        "https://scholar.google.com/scholar?q=foo",
        "https://ieeexplore.ieee.org/document/123456",
        "https://www.researchgate.net/publication/123_Foo",
        "https://arbitrary-publisher.example.com/paper.pdf",
    ]:
        p = _paper("x2024", url)
        assert f._resolve_pdf_url(p) is None, f"must skip {url}"


def test_resolve_pdf_url_handles_missing_url() -> None:
    """A paper without a PDF URL is skipped (Crossref-only papers usually)."""
    vs = _FakeVectorStore()
    f = FullTextFetcher(vector_store=vs)
    assert f._resolve_pdf_url(_paper("x2024", None)) is None


def test_chunk_splits_long_paragraphs_under_cap() -> None:
    """A single huge paragraph must be split at sentence boundaries."""
    vs = _FakeVectorStore()
    f = FullTextFetcher(vector_store=vs)
    # Build a paragraph of ~5 000 chars made of many short sentences.
    big_para = " ".join(f"Sentence number {i} contains some text content." for i in range(100))
    chunks = f._chunk(big_para)
    assert len(chunks) > 1, "long paragraph must be split into multiple chunks"
    for c in chunks:
        assert len(c) <= 1800, f"chunk exceeded cap: {len(c)} chars"


def test_chunk_drops_short_paragraphs() -> None:
    """Page numbers and headers (very short paragraphs) must be filtered out."""
    vs = _FakeVectorStore()
    f = FullTextFetcher(vector_store=vs)
    text = (
        "Page 1\n\n"
        + ("Real content paragraph. " * 30)
        + "\n\nfoo\n\n"
        + ("Another real paragraph. " * 30)
    )
    chunks = f._chunk(text)
    # Two real paragraphs survive, the "Page 1" and "foo" headers are dropped.
    assert len(chunks) == 2
    for c in chunks:
        assert "Real content" in c or "Another real" in c


@pytest.mark.asyncio
async def test_ingest_skips_non_pdf_responses() -> None:
    """If the OA URL redirects to an HTML interstitial, we must not embed it.

    A common failure mode: publisher OA mirrors sometimes 200-OK an HTML
    "consent to download" page. Without the %PDF sniff we'd embed HTML garbage.
    """
    vs = _FakeVectorStore()
    f = FullTextFetcher(vector_store=vs)
    p = _paper("html2024", "https://arxiv.org/pdf/2401.00001")

    with respx.mock:
        respx.get("https://arxiv.org/pdf/2401.00001").mock(
            return_value=httpx.Response(200, content=b"<html>Not a PDF</html>")
        )
        ingested = await f.ingest(TEST_PROJECT_ID, [p])

    assert ingested == 0
    assert vs.upserts == []  # nothing was embedded


@pytest.mark.asyncio
async def test_ingest_embeds_a_real_pdf_into_namespace() -> None:
    """End-to-end happy path with a mocked PDF byte stream and pypdf patched.

    pypdf is hard to feed real bytes for in a unit test, so we patch the text-
    extraction step and only exercise the HTTP + chunk + embed path.
    """
    vs = _FakeVectorStore()
    f = FullTextFetcher(vector_store=vs)
    p = _paper("alpha2024", "https://arxiv.org/pdf/2401.00001")

    # A minimal valid-looking PDF byte string (starts with %PDF so the sniff
    # passes); pypdf is patched so we don't need a real PDF.
    pdf_bytes = b"%PDF-1.4\n%fakecontent"
    long_text = ("This is a real paragraph from the paper. " * 40) + (
        "\n\nAnd here is a second paragraph with more content. " * 40
    )

    with respx.mock:
        respx.get("https://arxiv.org/pdf/2401.00001").mock(
            return_value=httpx.Response(200, content=pdf_bytes)
        )
        with patch.object(FullTextFetcher, "_extract_text", staticmethod(lambda _b, _k: long_text)):
            ingested = await f.ingest(TEST_PROJECT_ID, [p])

    assert ingested == 1
    assert len(vs.upserts) == 1
    namespace, docs = vs.upserts[0]
    assert namespace == str(TEST_PROJECT_ID)
    assert all(d["id"] for d in docs)
    # Chunk ids are <citation_key>:<index>, so a re-ingest replaces (Chroma
    # upsert semantics) rather than duplicating.
    assert all(str(d["id"]).startswith("alpha2024:") for d in docs)


@pytest.mark.asyncio
async def test_ingest_returns_zero_on_empty_pool() -> None:
    """No papers in → 0 ingested, no HTTP calls."""
    vs = _FakeVectorStore()
    f = FullTextFetcher(vector_store=vs)
    assert await f.ingest(TEST_PROJECT_ID, []) == 0
    assert vs.upserts == []
