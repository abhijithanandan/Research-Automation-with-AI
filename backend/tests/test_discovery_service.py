"""Tests for the DiscoveryService adapters (httpx mocked via respx).

Uses respx to mock HTTP calls so no network is required.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.services.discovery import ArXivAdapter, SemanticScholarAdapter

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


@pytest.mark.asyncio
async def test_arxiv_adapter_parses_feed() -> None:
    """ArXivAdapter should parse atom feed and return Paper objects."""
    with respx.mock:
        respx.get("http://export.arxiv.org/api/query").mock(
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
        respx.get("http://export.arxiv.org/api/query").mock(return_value=httpx.Response(503))
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
