"""Tests for the Unpaywall enricher (app.services.unpaywall).

Safety-critical behaviours:
  - No HTTP calls when ``email`` is unset (Unpaywall ToS forbids anonymous use).
  - Papers that already have a ``pdf_url`` are passed through untouched.
  - Non-DOI external_ids (arXiv ids, PMC ids) are skipped — Unpaywall only
    indexes by DOI.
  - A 404 (paper not in the index) is non-fatal — paper passes through.
  - When ``is_oa`` is true and ``best_oa_location.url_for_pdf`` is set, the
    paper's ``pdf_url`` is populated with that URL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from app.models.schemas import Paper
from app.services.unpaywall import UnpaywallEnricher

TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000012")


def _paper(
    citation_key: str,
    external_id: str,
    pdf_url: str | None = None,
) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=TEST_PROJECT_ID,
        source="crossref",
        external_id=external_id,
        title=f"Title for {citation_key}",
        authors=["Smith, J"],
        year=2024,
        abstract=None,
        pdf_url=pdf_url,  # type: ignore[arg-type]
        citation_key=citation_key,
        approved=True,
        added_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_no_op_when_email_is_missing() -> None:
    """Without an email Unpaywall must not be called — pass papers through."""
    enricher = UnpaywallEnricher(email="")
    p = _paper("x2024", "10.5555/foo.2024")
    # No respx mock active — any HTTP call would raise. The assertion is that
    # we return without making one.
    result = await enricher.enrich([p])
    assert result == [p]


@pytest.mark.asyncio
async def test_passes_through_papers_with_existing_pdf() -> None:
    """If a paper already has pdf_url, Unpaywall isn't queried — saves quota."""
    enricher = UnpaywallEnricher(email="dev@example.com")
    p = _paper("x2024", "10.5555/foo.2024", pdf_url="https://arxiv.org/pdf/2401.00001")
    with respx.mock:
        # Asserting respx.mock doesn't register any call below.
        result = await enricher.enrich([p])
    assert len(result) == 1
    assert str(result[0].pdf_url) == "https://arxiv.org/pdf/2401.00001"


@pytest.mark.asyncio
async def test_skips_non_doi_external_ids() -> None:
    """arXiv ids and PMC ids look nothing like a DOI — must be passed through."""
    enricher = UnpaywallEnricher(email="dev@example.com")
    p = _paper("arxiv2024", "arxiv:2401.00001")
    with respx.mock:
        # No HTTP mock — if the enricher tried to call, the test would fail.
        result = await enricher.enrich([p])
    assert result[0].pdf_url is None


@pytest.mark.asyncio
async def test_404_passes_through_paper_unchanged() -> None:
    """A DOI not in the Unpaywall index returns 404 — that is normal, not an error."""
    enricher = UnpaywallEnricher(email="dev@example.com")
    p = _paper("x2024", "10.5555/notfound.2024")
    with respx.mock:
        respx.get("https://api.unpaywall.org/v2/10.5555/notfound.2024").mock(
            return_value=httpx.Response(404, json={"message": "Not found"})
        )
        result = await enricher.enrich([p])
    assert result[0].pdf_url is None


@pytest.mark.asyncio
async def test_populates_pdf_url_from_best_oa_location() -> None:
    """The happy path: is_oa + url_for_pdf present → paper gets that URL."""
    enricher = UnpaywallEnricher(email="dev@example.com")
    p = _paper("x2024", "10.5555/oa.2024")
    response_body = {
        "doi": "10.5555/oa.2024",
        "is_oa": True,
        "best_oa_location": {
            "url_for_pdf": "https://oa-mirror.example.com/papers/2024-001.pdf",
            "host_type": "repository",
            "license": "cc-by",
        },
    }
    with respx.mock:
        respx.get("https://api.unpaywall.org/v2/10.5555/oa.2024").mock(
            return_value=httpx.Response(200, json=response_body)
        )
        result = await enricher.enrich([p])

    assert len(result) == 1
    assert str(result[0].pdf_url) == "https://oa-mirror.example.com/papers/2024-001.pdf"


@pytest.mark.asyncio
async def test_is_not_oa_passes_through_unchanged() -> None:
    """Paper is in Unpaywall but is not OA → no PDF URL, paper unchanged."""
    enricher = UnpaywallEnricher(email="dev@example.com")
    p = _paper("x2024", "10.5555/closed.2024")
    response_body = {"doi": "10.5555/closed.2024", "is_oa": False}
    with respx.mock:
        respx.get("https://api.unpaywall.org/v2/10.5555/closed.2024").mock(
            return_value=httpx.Response(200, json=response_body)
        )
        result = await enricher.enrich([p])
    assert result[0].pdf_url is None


@pytest.mark.asyncio
async def test_network_failure_passes_through_unchanged() -> None:
    """A 5xx or timeout must not sink the workflow — paper passes through."""
    enricher = UnpaywallEnricher(email="dev@example.com")
    p = _paper("x2024", "10.5555/flaky.2024")
    with respx.mock:
        respx.get("https://api.unpaywall.org/v2/10.5555/flaky.2024").mock(
            return_value=httpx.Response(500)
        )
        result = await enricher.enrich([p])
    assert result[0].pdf_url is None
