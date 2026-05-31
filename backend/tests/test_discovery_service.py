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
async def test_arxiv_rejects_xml_bomb() -> None:
    """W1-A4: arXiv parser uses defusedxml — billion-laughs entity expansion
    is rejected at parse time. The adapter returns an empty list instead of
    exhausting memory. Confirms bandit B314 is no longer applicable."""
    # 9-level nested entity expansion = 10^9 'lol' bytes if expanded. defusedxml
    # raises EntitiesForbidden on parse; the adapter must NOT expand it.
    xml_bomb = """<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>&lol4;</title></entry></feed>"""
    # CodeRabbit follow-up: adapters now raise SourceUnavailableError on retry
    # exhaustion (the router catches it and counts it as a failure). The
    # invariant under test here is "NO memory blow-up, NO crash from the
    # XML-bomb itself" — defusedxml refusing the DTD inside _safe_fromstring
    # causes the adapter to log arxiv_bad_xml and return []; if tenacity
    # exhausts (depends on test wiring), the adapter raises SourceUnavailableError.
    # Both outcomes are acceptable here: the bomb did not expand.
    from app.services.discovery import SourceUnavailableError

    with respx.mock:
        respx.get("https://export.arxiv.org/api/query").mock(
            return_value=httpx.Response(200, text=xml_bomb)
        )
        adapter = ArXivAdapter()
        # Disable tenacity retries so a parser-rejection failure surfaces
        # immediately rather than triggering 3 retries.
        adapter._search_with_retry.retry.stop = lambda *_: True  # type: ignore[attr-defined]
        async with httpx.AsyncClient() as client:
            try:
                papers = await adapter.search("xxe", max_results=10, client=client)
            except SourceUnavailableError:
                papers = []  # retry-exhausted is the alternate acceptable outcome
    assert papers == []


@pytest.mark.asyncio
async def test_arxiv_adapter_handles_5xx() -> None:
    """ArXivAdapter should raise SourceUnavailableError on retry-exhausted 5xx
    (CodeRabbit follow-up). The discovery router catches this and counts it
    toward the fail-fast `consecutive_failures`. Previously the adapter
    swallowed RetryError -> [] and the router could not distinguish it from
    a legitimate zero-hit query."""
    import pytest as _pytest

    from app.services.discovery import SourceUnavailableError

    with respx.mock:
        respx.get("https://export.arxiv.org/api/query").mock(return_value=httpx.Response(503))
        adapter = ArXivAdapter()
        adapter._search_with_retry.retry.stop = lambda *_: True  # type: ignore[attr-defined]
        async with httpx.AsyncClient() as client:
            with _pytest.raises(SourceUnavailableError):
                await adapter.search("query", max_results=10, client=client)


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
    """A persistent 429 must raise SourceUnavailableError (CodeRabbit follow-up).
    The discovery router catches it and counts it toward fail-fast; the
    surviving sources (arXiv/Crossref/...) still run their queries."""
    import pytest as _pytest

    from app.services.discovery import SourceUnavailableError

    with respx.mock:
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(429)
        )
        adapter = SemanticScholarAdapter()
        adapter._search_with_retry.retry.stop = lambda *_: True  # type: ignore[attr-defined]
        async with httpx.AsyncClient() as client:
            with _pytest.raises(SourceUnavailableError):
                await adapter.search("query", max_results=10, client=client)


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
    """A 5xx after retries exhausted must raise SourceUnavailableError
    (CodeRabbit follow-up). The router converts that to fail-fast bookkeeping."""
    import pytest as _pytest

    from app.services.discovery import SourceUnavailableError

    with respx.mock:
        respx.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search").mock(
            return_value=httpx.Response(503)
        )
        adapter = EuropePMCAdapter()
        adapter._search_with_retry.retry.stop = lambda *_: True  # type: ignore[attr-defined]
        async with httpx.AsyncClient() as client:
            with _pytest.raises(SourceUnavailableError):
                await adapter.search("query", max_results=10, client=client)


# ---------------------------------------------------------------------------
# W2-S3 — Retry-After honored on 429
# ---------------------------------------------------------------------------


def test_sleep_for_retry_after_parses_integer_delta() -> None:
    """Bare integer (RFC 7231 delta-seconds) is parsed and used."""
    import asyncio

    from app.services.discovery import _sleep_for_retry_after

    captured_delays: list[float] = []

    async def _fake_sleep(d: float) -> None:
        captured_delays.append(d)

    resp = httpx.Response(429, headers={"Retry-After": "5"})

    async def _run() -> None:
        from unittest.mock import patch as _patch

        with _patch("app.services.discovery.asyncio.sleep", side_effect=_fake_sleep):
            await _sleep_for_retry_after(resp)

    asyncio.run(_run())
    assert captured_delays == [5.0]


def test_sleep_for_retry_after_caps_at_60s() -> None:
    """A server sending Retry-After: 9999 must not hang the workflow."""
    import asyncio

    from app.services.discovery import _RETRY_AFTER_MAX_S, _sleep_for_retry_after

    captured: list[float] = []

    async def _fake_sleep(d: float) -> None:
        captured.append(d)

    resp = httpx.Response(429, headers={"Retry-After": "9999"})

    async def _run() -> None:
        from unittest.mock import patch as _patch

        with _patch("app.services.discovery.asyncio.sleep", side_effect=_fake_sleep):
            await _sleep_for_retry_after(resp)

    asyncio.run(_run())
    assert captured == [_RETRY_AFTER_MAX_S]


def test_sleep_for_retry_after_no_header_is_noop() -> None:
    """No Retry-After header → no sleep (tenacity backoff stays authoritative)."""
    import asyncio

    from app.services.discovery import _sleep_for_retry_after

    captured: list[float] = []

    async def _fake_sleep(d: float) -> None:
        captured.append(d)

    resp = httpx.Response(429)

    async def _run() -> None:
        from unittest.mock import patch as _patch

        with _patch("app.services.discovery.asyncio.sleep", side_effect=_fake_sleep):
            await _sleep_for_retry_after(resp)

    asyncio.run(_run())
    assert captured == []


def test_sleep_for_retry_after_handles_http_date() -> None:
    """RFC-1123 HTTP-date form is rarer but compliant; should convert to a
    positive delta seconds and sleep that long."""
    import asyncio
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    from app.services.discovery import _sleep_for_retry_after

    captured: list[float] = []

    async def _fake_sleep(d: float) -> None:
        captured.append(d)

    future = datetime.now(tz=UTC) + timedelta(seconds=10)
    resp = httpx.Response(429, headers={"Retry-After": format_datetime(future, usegmt=True)})

    async def _run() -> None:
        from unittest.mock import patch as _patch

        with _patch("app.services.discovery.asyncio.sleep", side_effect=_fake_sleep):
            await _sleep_for_retry_after(resp)

    asyncio.run(_run())
    assert len(captured) == 1
    # Near 10s (slack for wall-clock drift between building/reading the date).
    assert 8.0 <= captured[0] <= 10.5


@pytest.mark.asyncio
async def test_arxiv_429_with_retry_after_sleeps_before_reraise() -> None:
    """Integration: arXiv adapter sees a 429 with Retry-After: 3, calls
    _sleep_for_retry_after, then tenacity retries against the 200."""
    from unittest.mock import patch as _patch

    captured_sleeps: list[float] = []

    async def _fake_sleep(d: float) -> None:
        captured_sleeps.append(d)

    # Other tests in this file monkey-patch _search_with_retry.retry.stop to
    # short-circuit tenacity. That mutation is sticky on the class-level
    # wrapper. Save and restore so this test runs deterministically regardless
    # of order.
    from tenacity import stop_after_attempt

    original_stop = ArXivAdapter._search_with_retry.retry.stop  # type: ignore[attr-defined]
    ArXivAdapter._search_with_retry.retry.stop = stop_after_attempt(3)  # type: ignore[attr-defined]
    try:
        with respx.mock:
            respx.get("https://export.arxiv.org/api/query").mock(
                side_effect=[
                    httpx.Response(429, headers={"Retry-After": "3"}, text=""),
                    httpx.Response(200, text=ARXIV_ATOM_RESPONSE),
                ]
            )
            adapter = ArXivAdapter()
            async with httpx.AsyncClient() as client:
                with _patch("app.services.discovery.asyncio.sleep", side_effect=_fake_sleep):
                    papers = await adapter.search("query", max_results=10, client=client)
    finally:
        ArXivAdapter._search_with_retry.retry.stop = original_stop  # type: ignore[attr-defined]

    assert len(papers) == 2
    # The Retry-After: 3 sleep must have fired (alongside any tenacity backoff,
    # which goes through tenacity.nap and doesn't show up here).
    assert 3.0 in captured_sleeps
