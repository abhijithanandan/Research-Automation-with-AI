"""Tests for the DiscoveryService adapters (httpx mocked via respx).

Uses respx to mock HTTP calls so no network is required.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.discovery import (
    ArXivAdapter,
    CoreAdapter,
    CrossrefAdapter,
    EuropePMCAdapter,
    SemanticScholarAdapter,
)

ARXIV_ATOM_RESPONSE = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2301.00001v1</id>
    <title>Attention Is All You Need</title>
    <summary>A seminal paper on transformers.</summary>
    <published>2021-06-12T00:00:00Z</published>
    <author><name>Vaswani, A</name></author>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2301.00002v1</id>
    <title>BERT: Pre-training of Deep Bidirectional Transformers</title>
    <summary>BERT paper abstract.</summary>
    <published>2022-10-11T00:00:00Z</published>
    <author><name>Devlin, J</name></author>
  </entry>
</feed>
"""

SS_JSON_RESPONSE = {
    "data": [
        {
            "paperId": "ss-001",
            "externalIds": {"DOI": "10.1234/ss.2023"},
            "title": "Semantic Scholar Paper",
            "authors": [{"name": "Jones, B"}],
            "year": 2023,
            "abstract": "Abstract text.",
            "openAccessPdf": {"url": "https://example.com/paper.pdf"},
        }
    ]
}

CROSSREF_JSON_RESPONSE = {
    "message": {
        "items": [
            {
                "DOI": "10.1145/crossref.2024",
                "title": ["A Crossref-Indexed Paper"],
                "author": [
                    {"given": "Alice", "family": "Walker"},
                    {"given": "Bob", "family": "Singh"},
                ],
                "issued": {"date-parts": [[2024, 3]]},
                "abstract": "<jats:p>An abstract wrapped in JATS XML.</jats:p>",
                "is-referenced-by-count": 12,
            }
        ]
    }
}


@pytest.mark.asyncio
async def test_arxiv_adapter_parses_feed() -> None:
    """ArXivAdapter should parse atom feed and return Paper objects."""
    with respx.mock:
        respx.get("https://export.arxiv.org/api/query").mock(
            return_value=httpx.Response(200, text=ARXIV_ATOM_RESPONSE)
        )
        adapter = ArXivAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("transformers", max_results=10, client=client)

    assert len(papers) == 2
    titles = {p.title for p in papers}
    assert "Attention Is All You Need" in titles
    assert "BERT: Pre-training of Deep Bidirectional Transformers" in titles
    assert all(p.source == "arxiv" for p in papers)
    assert all(not p.approved for p in papers)


@pytest.mark.asyncio
async def test_arxiv_adapter_handles_5xx() -> None:
    """ArXivAdapter should return empty list on 5xx (non-fatal per agent contract)."""
    with respx.mock:
        respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(503))
        adapter = ArXivAdapter()
        # Disable tenacity retries for the test to keep it fast.
        adapter._search_with_retry.retry.stop = lambda *_: True  # type: ignore[attr-defined]
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("query", max_results=10, client=client)

    assert papers == []


@pytest.mark.asyncio
async def test_semantic_scholar_adapter_parses_response() -> None:
    """SemanticScholarAdapter should parse JSON and return Paper objects."""
    with respx.mock:
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(200, json=SS_JSON_RESPONSE)
        )
        adapter = SemanticScholarAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("test query", max_results=10, client=client)

    assert len(papers) == 1
    assert papers[0].title == "Semantic Scholar Paper"
    assert papers[0].source == "semantic_scholar"
    assert papers[0].external_id == "10.1234/ss.2023"
    assert not papers[0].approved


@pytest.mark.asyncio
async def test_semantic_scholar_adapter_handles_429_exhaustion() -> None:
    """A persistent 429 must degrade to [] (so arXiv/Crossref still apply)."""
    with respx.mock:
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(429)
        )
        adapter = SemanticScholarAdapter()
        # Disable tenacity retries to keep the test fast.
        adapter._search_with_retry.retry.stop = lambda *_: True  # type: ignore[attr-defined]
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("query", max_results=10, client=client)

    assert papers == []


@pytest.mark.asyncio
async def test_crossref_adapter_parses_response() -> None:
    """CrossrefAdapter should parse the REST response and return Paper objects."""
    with respx.mock:
        respx.get("https://api.crossref.org/works").mock(
            return_value=httpx.Response(200, json=CROSSREF_JSON_RESPONSE)
        )
        adapter = CrossrefAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("test query", max_results=10, client=client)

    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == "A Crossref-Indexed Paper"
    assert paper.source == "crossref"
    assert paper.external_id == "10.1145/crossref.2024"
    assert paper.authors == ["Alice Walker", "Bob Singh"]
    assert paper.year == 2024
    # JATS XML tags must be stripped from the abstract.
    assert paper.abstract == "An abstract wrapped in JATS XML."
    assert paper.citation_count == 12
    assert not paper.approved


@pytest.mark.asyncio
async def test_crossref_adapter_handles_4xx() -> None:
    """CrossrefAdapter should return [] on a 4xx client error (non-fatal)."""
    with respx.mock:
        respx.get("https://api.crossref.org/works").mock(return_value=httpx.Response(400))
        adapter = CrossrefAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("query", max_results=10, client=client)

    assert papers == []


# ---------------------------------------------------------------------------
# CORE adapter
# ---------------------------------------------------------------------------

CORE_JSON_RESPONSE = {
    "results": [
        {
            "id": 12345,
            "doi": "10.5555/core.2024.001",
            "title": "A CORE-Indexed Paper",
            "authors": [{"name": "Liu, Wei"}, {"name": "Garcia, M."}],
            "yearPublished": 2024,
            "abstract": "Abstract harvested from an institutional repository.",
            "downloadUrl": "https://example-repo.edu/papers/12345.pdf",
            "citationCount": 7,
        }
    ]
}


@pytest.mark.asyncio
async def test_core_adapter_no_op_without_api_key() -> None:
    """Without an API key the CORE adapter must skip cleanly — no HTTP calls."""
    adapter = CoreAdapter(api_key="")
    async with httpx.AsyncClient() as client:
        papers = await adapter.search("transformers", max_results=10, client=client)
    assert papers == []


@pytest.mark.asyncio
async def test_core_adapter_parses_response() -> None:
    """CORE adapter must parse the v3 search response into Paper objects."""
    with respx.mock:
        respx.get("https://api.core.ac.uk/v3/search/works").mock(
            return_value=httpx.Response(200, json=CORE_JSON_RESPONSE)
        )
        adapter = CoreAdapter(api_key="fake-test-key")
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("test", max_results=10, client=client)

    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == "A CORE-Indexed Paper"
    assert paper.source == "core"
    assert paper.external_id == "10.5555/core.2024.001"
    assert paper.authors == ["Liu, Wei", "Garcia, M."]
    assert paper.year == 2024
    assert str(paper.pdf_url) == "https://example-repo.edu/papers/12345.pdf"
    assert paper.citation_count == 7
    assert not paper.approved


@pytest.mark.asyncio
async def test_core_adapter_handles_4xx() -> None:
    """A 4xx must degrade to [] (not blow up the discovery run)."""
    with respx.mock:
        respx.get("https://api.core.ac.uk/v3/search/works").mock(return_value=httpx.Response(400))
        adapter = CoreAdapter(api_key="fake-test-key")
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("query", max_results=10, client=client)
    assert papers == []


# ---------------------------------------------------------------------------
# Europe PMC adapter
# ---------------------------------------------------------------------------

EUROPE_PMC_JSON_RESPONSE = {
    "resultList": {
        "result": [
            {
                "id": "PMC9999999",
                "pmcid": "PMC9999999",
                "doi": "10.5555/epmc.2024.001",
                "title": "A Biomedical Paper.",
                "authorString": "Smith J, Devi P, Tan H",
                "pubYear": "2024",
                "abstractText": "Abstract for the biomedical study.",
                "citedByCount": 42,
            }
        ]
    }
}


@pytest.mark.asyncio
async def test_europe_pmc_adapter_parses_response() -> None:
    """Europe PMC adapter parses authorString, year, and synthesises a PMC PDF URL."""
    with respx.mock:
        respx.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(
            return_value=httpx.Response(200, json=EUROPE_PMC_JSON_RESPONSE)
        )
        adapter = EuropePMCAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("CRISPR", max_results=10, client=client)

    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == "A Biomedical Paper"
    assert paper.source == "europe_pmc"
    assert paper.external_id == "10.5555/epmc.2024.001"
    assert paper.authors == ["Smith J", "Devi P", "Tan H"]
    assert paper.year == 2024
    assert paper.pdf_url is not None
    assert "PMC9999999" in str(paper.pdf_url)
    assert paper.citation_count == 42


@pytest.mark.asyncio
async def test_semantic_scholar_strips_dead_ieee_ielx_pdf_urls() -> None:
    """Stale /ielx7/ URLs from S2 must be dropped — they 404 unconditionally.

    Letting them through put broken links into the UI (the user observed this
    when every Semantic Scholar paper from IEEE Xplore 404'd on click).
    """
    body = {
        "data": [
            {
                "paperId": "ieee-001",
                "externalIds": {"DOI": "10.1109/ACCESS.2022.3226629"},
                "title": "An IEEE Paper With a Dead OA Link",
                "authors": [{"name": "Ansari, Y"}],
                "year": 2022,
                "abstract": "An abstract.",
                "openAccessPdf": {
                    "url": "https://ieeexplore.ieee.org/ielx7/6287639/6514899/09969608.pdf"
                },
            }
        ]
    }
    with respx.mock:
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(200, json=body)
        )
        adapter = SemanticScholarAdapter()
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("ieee", max_results=10, client=client)

    assert len(papers) == 1
    # The dead URL must be filtered. The paper survives — just without a PDF.
    assert papers[0].pdf_url is None
    assert papers[0].external_id == "10.1109/ACCESS.2022.3226629"


@pytest.mark.asyncio
async def test_europe_pmc_adapter_handles_5xx() -> None:
    """A 5xx must degrade to [] after retries are exhausted."""
    with respx.mock:
        respx.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(
            return_value=httpx.Response(503)
        )
        adapter = EuropePMCAdapter()
        # Disable tenacity retries to keep the test fast.
        adapter._search_with_retry.retry.stop = lambda *_: True  # type: ignore[attr-defined]
        async with httpx.AsyncClient() as client:
            papers = await adapter.search("query", max_results=10, client=client)
    assert papers == []
