"""Regression tests for the M3 reliability gate.

Coverage:
  M3-B: vector_store._parse_url rejects schemes outside {http, https}.
  M3-B: fulltext fetcher rejects responses whose Content-Type is HTML
        (publisher paywall interstitial served as 200 OK) BEFORE reading
        the body — the existing %PDF magic check is the second layer.
"""

from __future__ import annotations

import pytest

from app.services.vector_store import _parse_url

# ---------------------------------------------------------------------------
# M3-B: vector_store URL scheme whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://chroma:8000",
        "https://chroma:8001",
        "chroma:8001",  # no scheme — defaults to http
        "http://[::1]:8001",  # IPv6
    ],
)
def test_parse_url_accepts_http_and_https(url: str) -> None:
    # Doesn't raise → scheme is in the whitelist.
    host, port = _parse_url(url)
    assert host
    assert port > 0


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://attacker.example.com",
        "gopher://10.0.0.1",
        "ws://chroma:8000",  # raw ws scheme — must use http(s) only
    ],
)
def test_parse_url_rejects_non_http_schemes(url: str) -> None:
    """A misconfigured VECTOR_DB_URL must fail loud, not silently downgrade
    to a default host. The error message includes the offending scheme so
    operators can fix the env var without spelunking the source.

    Note: schemeless inputs like ``javascript:alert(1)`` get an ``http://``
    prefix during normalization (the bare-host shorthand), so they pass
    the whitelist as ``http`` — not great but also not exploitable since
    the result is just a host of ``javascript`` which can't resolve. The
    whitelist guards declared-scheme footguns specifically.
    """
    with pytest.raises(ValueError, match="http or https"):
        _parse_url(url)


# ---------------------------------------------------------------------------
# M3-B: fulltext fetcher content-type filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fulltext_fetcher_rejects_html_content_type() -> None:
    """A 200 OK with Content-Type=text/html is the classic publisher
    paywall interstitial. Bail before reading the body so we don't waste
    bytes on something we can't parse."""
    import httpx
    import respx

    from app.services.fulltext_fetcher import FullTextFetcher

    paywall_html = "<html><body>Please sign in</body></html>"
    pdf_url = "https://example.com/fake.pdf"

    with respx.mock(assert_all_called=False) as router:
        router.get(pdf_url).respond(
            status_code=200,
            content=paywall_html.encode(),
            headers={"content-type": "text/html; charset=utf-8"},
        )
        fetcher = FullTextFetcher.__new__(FullTextFetcher)
        async with httpx.AsyncClient() as client:
            result = await fetcher._download_pdf(client, pdf_url, "fake2024")
    assert result is None


@pytest.mark.asyncio
async def test_fulltext_fetcher_accepts_pdf_content_type() -> None:
    """A response with Content-Type=application/pdf passes the filter and
    proceeds to the %PDF magic check. Body must still start with %PDF."""
    import httpx
    import respx

    from app.services.fulltext_fetcher import FullTextFetcher

    fake_pdf = b"%PDF-1.4\nfake but well-shaped\n%%EOF"
    pdf_url = "https://example.com/real.pdf"

    with respx.mock(assert_all_called=False) as router:
        router.get(pdf_url).respond(
            status_code=200,
            content=fake_pdf,
            headers={"content-type": "application/pdf"},
        )
        fetcher = FullTextFetcher.__new__(FullTextFetcher)
        async with httpx.AsyncClient() as client:
            result = await fetcher._download_pdf(client, pdf_url, "real2024")
    assert result == fake_pdf


@pytest.mark.asyncio
async def test_fulltext_fetcher_accepts_octet_stream_content_type() -> None:
    """Some OA mirrors serve PDFs as application/octet-stream. Accept that
    AND the magic-byte fallback catches anyone serving a header lie."""
    import httpx
    import respx

    from app.services.fulltext_fetcher import FullTextFetcher

    fake_pdf = b"%PDF-1.5\nfake\n%%EOF"
    pdf_url = "https://example.com/oa.pdf"

    with respx.mock(assert_all_called=False) as router:
        router.get(pdf_url).respond(
            status_code=200,
            content=fake_pdf,
            headers={"content-type": "application/octet-stream"},
        )
        fetcher = FullTextFetcher.__new__(FullTextFetcher)
        async with httpx.AsyncClient() as client:
            result = await fetcher._download_pdf(client, pdf_url, "oa2024")
    assert result == fake_pdf
