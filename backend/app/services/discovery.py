"""External academic source adapters for the Librarian agent.

All HTTP calls go through this module — agents must never call httpx directly
(see docs/agents/librarian.md §Invariants). Two adapters are wired in Phase 1:
  - SemanticScholarAdapter  (JSON API v1 graph endpoint)
  - ArXivAdapter            (Atom feed API)

Each adapter implements `SourceAdapter` (a Protocol). The `DiscoveryService`
fans them out in parallel and merges results.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

import httpx
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

from app.models.schemas import Paper
from app.utils.logging import get_logger

_log = get_logger(__name__)

# Shared async client (re-used across adapter calls within a single request).
_HTTP_TIMEOUT = httpx.Timeout(30.0)


def _safe_json(resp: httpx.Response, source: str, query: str) -> dict[str, object] | None:
    """Parse a JSON response defensively.

    A 200 OK with a non-JSON body (HTML error page, truncated stream, mid-
    response disconnect) used to raise an uncaught ``JSONDecodeError`` that
    crashed the source's whole query lane (audit finding #7). Returns ``None``
    on any parse failure; callers treat that as "no results from this query"
    and the rest of the discovery run continues.
    """
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        _log.warning("source_bad_json", source=source, query=query, error=str(exc))
        return None
    if not isinstance(data, dict):
        _log.warning("source_unexpected_json_shape", source=source, query=query)
        return None
    return data


def _sanitise_pdf_url(url: str | None) -> str | None:
    """Reject ``openAccessPdf.url`` values we know are dead.

    Semantic Scholar (and occasionally CORE) carry years-stale OA links in
    their indexes. The most common offender is IEEE Xplore's old ``/ielx7/``
    CDN endpoint, which returns 404 for every article today — IEEE serves
    PDFs only via ``/stamp/stamp.jsp?…`` now, and that path requires a session.
    Returning ``None`` here lets the Unpaywall enricher search for a working
    OA copy elsewhere, and the frontend falls back to the DOI link.
    """
    if url is None:
        return None
    url_stripped = url.strip()
    if not url_stripped:
        return None
    # IEEE's deprecated /ielx7/<group>/<issue>/<artnum>.pdf — 404 since 2022.
    if "ieeexplore.ieee.org/ielx" in url_stripped:
        return None
    return url_stripped


# ---------------------------------------------------------------------------
# Protocol that every source adapter must satisfy
# ---------------------------------------------------------------------------


class SourceAdapter(Protocol):
    async def search(
        self, query: str, max_results: int, client: httpx.AsyncClient
    ) -> list[Paper]: ...


# ---------------------------------------------------------------------------
# Semantic Scholar adapter
# ---------------------------------------------------------------------------

_SS_BASE = "https://api.semanticscholar.org/graph/v1"
_SS_FIELDS = "paperId,externalIds,title,authors,year,abstract,citationCount,openAccessPdf"


class SemanticScholarAdapter:
    """Queries the Semantic Scholar Graph API v1.

    An optional API key raises the rate limit from 100 to 1 000 req/min.
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"x-api-key": self._api_key}
        return {}

    async def search(self, query: str, max_results: int, client: httpx.AsyncClient) -> list[Paper]:
        """Public entry point — wraps the retried inner call, returns [] on exhaustion.

        Without an API key Semantic Scholar throttles aggressively (HTTP 429). If
        all retries are exhausted we degrade to an empty list rather than letting
        the RetryError drop the whole discovery run — arXiv / Crossref still apply.
        """
        try:
            return await self._search_with_retry(query, max_results, client)
        except RetryError:
            _log.warning("semantic_scholar_retry_exhausted", query=query)
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def _search_with_retry(
        self, query: str, max_results: int, client: httpx.AsyncClient
    ) -> list[Paper]:
        params: dict[str, str | int] = {
            "query": query,
            "limit": min(max_results, 100),
            "fields": _SS_FIELDS,
        }
        try:
            resp = await client.get(
                f"{_SS_BASE}/paper/search",
                params=params,
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            _log.warning("semantic_scholar_error", status=status_code, query=query)
            # Re-raise on transient errors so tenacity retries; swallow 4xx client errors.
            if status_code in (429,) or status_code >= 500:
                raise
            return []

        data = _safe_json(resp, source="semantic_scholar", query=query)
        if data is None:
            return []
        papers: list[Paper] = []
        now = datetime.now(tz=UTC)
        raw_items = data.get("data")
        items: list[object] = raw_items if isinstance(raw_items, list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            ext_ids: dict[str, str] = item.get("externalIds") or {}
            doi = ext_ids.get("DOI", "")
            arxiv_id = ext_ids.get("ArXiv", "")
            external_id = doi or arxiv_id or item.get("paperId", "")
            if not external_id:
                continue

            pdf_url: str | None = None
            oap = item.get("openAccessPdf")
            if oap and oap.get("url"):
                # Strip stale IEEE /ielx7/ links — see _sanitise_pdf_url docstring.
                pdf_url = _sanitise_pdf_url(oap["url"])

            authors = [a.get("name", "") for a in (item.get("authors") or [])]
            papers.append(
                Paper(
                    id=uuid4(),
                    project_id=None,
                    source="semantic_scholar",
                    external_id=external_id,
                    title=item.get("title") or "",
                    authors=authors,
                    year=item.get("year"),
                    abstract=item.get("abstract") or None,
                    pdf_url=pdf_url,  # type: ignore[arg-type]
                    citation_key="",  # generated by Librarian after dedup
                    citation_count=item.get("citationCount"),
                    approved=False,
                    added_at=now,
                )
            )

        _log.info("semantic_scholar_results", query=query, count=len(papers))
        return papers


# ---------------------------------------------------------------------------
# ArXiv adapter
# ---------------------------------------------------------------------------

# Go straight to HTTPS — arXiv 301-redirects HTTP→HTTPS, and each redirect hop
# counts against their rate limit, doubling our effective request rate.
_ARXIV_BASE = "https://export.arxiv.org/api/query"
_ARXIV_NS = "http://www.w3.org/2005/Atom"
_ARXIV_OAI = "http://arxiv.org/schemas/atom"


class ArXivAdapter:
    """Queries the ArXiv Atom Feed API (no API key required)."""

    async def search(
        self,
        query: str,
        max_results: int,
        client: httpx.AsyncClient,
        categories: list[str] | None = None,
    ) -> list[Paper]:
        """Public entry point — wraps the retried inner call, returns [] on exhaustion."""
        try:
            return await self._search_with_retry(query, max_results, client, categories)
        except RetryError:
            _log.warning("arxiv_retry_exhausted", query=query)
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def _search_with_retry(
        self,
        query: str,
        max_results: int,
        client: httpx.AsyncClient,
        categories: list[str] | None = None,
    ) -> list[Paper]:
        # Build the search_query string with optional ArXiv category filters.
        search_expr = f"all:{query}"
        if categories:
            cat_expr = " OR ".join(f"cat:{c}" for c in categories[:5])
            search_expr = f"({search_expr}) AND ({cat_expr})"

        params: dict[str, str | int] = {
            "search_query": search_expr,
            "max_results": min(max_results, 100),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        try:
            resp = await client.get(_ARXIV_BASE, params=params, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            _log.warning("arxiv_error", status=status_code, query=query)
            if status_code in (429,) or status_code >= 500:
                raise
            return []

        # Defensive XML parse — arxiv occasionally returns truncated Atom
        # feeds, especially during their maintenance windows. A ParseError
        # would otherwise crash this whole query lane (audit finding #7).
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            _log.warning("arxiv_bad_xml", query=query, error=str(exc))
            return []
        ns = {"atom": _ARXIV_NS}
        papers: list[Paper] = []
        now = datetime.now(tz=UTC)

        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            title = (title_el.text or "").strip().replace("\n", " ") if title_el is not None else ""
            if not title:
                continue

            # ArXiv ID is the last segment of the <id> element URL.
            id_el = entry.find("atom:id", ns)
            arxiv_url = id_el.text.strip() if id_el is not None and id_el.text else ""
            arxiv_id = arxiv_url.split("/")[-1] if arxiv_url else ""
            if not arxiv_id:
                continue

            abstract_el = entry.find("atom:summary", ns)
            abstract = (
                abstract_el.text.strip() if abstract_el is not None and abstract_el.text else None
            )

            published_el = entry.find("atom:published", ns)
            year: int | None = None
            if published_el is not None and published_el.text:
                try:
                    year = int(published_el.text[:4])
                    if year < now.year - 5:  # Filter out papers older than 5 years
                        continue
                except ValueError:
                    pass

            authors: list[str] = []
            for a in entry.findall("atom:author", ns):
                name_el = a.find("atom:name", ns)
                if name_el is not None and name_el.text:
                    authors.append(name_el.text.strip())

            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

            papers.append(
                Paper(
                    id=uuid4(),
                    project_id=None,
                    source="arxiv",
                    external_id=arxiv_id,
                    title=title,
                    authors=authors,
                    year=year,
                    abstract=abstract,
                    pdf_url=pdf_url,  # type: ignore[arg-type]
                    citation_key="",  # generated by Librarian after dedup
                    approved=False,
                    added_at=now,
                )
            )

        _log.info("arxiv_results", query=query, count=len(papers))
        return papers


# ---------------------------------------------------------------------------
# Crossref adapter
# ---------------------------------------------------------------------------

_CROSSREF_BASE = "https://api.crossref.org/works"


class CrossrefAdapter:
    """Queries the Crossref REST API (BRD FR-2.1).

    Crossref has no API key; it asks callers to identify themselves via the
    User-Agent header for the faster "polite pool". No key is required.
    """

    def __init__(self, mailto: str = "") -> None:
        # An optional contact email puts requests in Crossref's polite pool.
        self._mailto = mailto

    def _headers(self) -> dict[str, str]:
        ua = "ResearchFlowAI/0.1 (https://github.com/researchflow-ai)"
        if self._mailto:
            ua += f"; mailto:{self._mailto}"
        return {"User-Agent": ua}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def search(self, query: str, max_results: int, client: httpx.AsyncClient) -> list[Paper]:
        params: dict[str, str | int] = {
            "query": query,
            "rows": min(max_results, 100),
            "select": "DOI,title,author,issued,abstract,is-referenced-by-count,URL",
        }
        try:
            resp = await client.get(
                _CROSSREF_BASE,
                params=params,
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            _log.warning("crossref_error", status=status_code, query=query)
            # Retry transient failures; swallow 4xx client errors.
            if status_code in (429,) or status_code >= 500:
                raise
            return []

        data = _safe_json(resp, source="crossref", query=query)
        if data is None:
            return []
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        raw_items = message.get("items") if isinstance(message, dict) else None
        items = raw_items if isinstance(raw_items, list) else []
        papers: list[Paper] = []
        now = datetime.now(tz=UTC)
        for item in items:
            doi = item.get("DOI", "")
            if not doi:
                continue

            # Crossref `title` is a list; take the first non-empty entry.
            title_list = item.get("title") or []
            title = (title_list[0] if title_list else "").strip()
            if not title:
                continue

            # Authors: {"given": "Jane", "family": "Smith"} → "Jane Smith".
            authors: list[str] = []
            for a in item.get("author") or []:
                given = (a.get("given") or "").strip()
                family = (a.get("family") or "").strip()
                full = f"{given} {family}".strip()
                if full:
                    authors.append(full)

            # `issued` carries the publication date as nested date-parts.
            year: int | None = None
            date_parts = (item.get("issued") or {}).get("date-parts") or []
            if date_parts and date_parts[0]:
                try:
                    year = int(date_parts[0][0])
                except (ValueError, TypeError, IndexError):
                    year = None

            # Crossref abstracts arrive as JATS XML — strip tags for plain text.
            abstract_raw = item.get("abstract")
            abstract = re.sub(r"<[^>]+>", "", abstract_raw).strip() if abstract_raw else None

            papers.append(
                Paper(
                    id=uuid4(),
                    project_id=None,
                    source="crossref",
                    external_id=doi,
                    title=title,
                    authors=authors,
                    year=year,
                    abstract=abstract,
                    pdf_url=None,
                    citation_key="",  # generated by Librarian after dedup
                    citation_count=item.get("is-referenced-by-count"),
                    approved=False,
                    added_at=now,
                )
            )

        _log.info("crossref_results", query=query, count=len(papers))
        return papers


# ---------------------------------------------------------------------------
# CORE adapter — api.core.ac.uk
# ---------------------------------------------------------------------------

_CORE_BASE = "https://api.core.ac.uk/v3/search/works"


class CoreAdapter:
    """Queries the CORE v3 search API (https://core.ac.uk).

    CORE aggregates ~280M open-access works from thousands of repositories.
    A free API key is required (register at https://core.ac.uk/services/api).
    Without a key the adapter is a no-op — it logs once per process and
    returns ``[]`` for every query, so the rest of the discovery pipeline
    keeps working.
    """

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key
        self._warned_no_key = False

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        return {"Authorization": f"Bearer {self._api_key}"}

    async def search(self, query: str, max_results: int, client: httpx.AsyncClient) -> list[Paper]:
        """Public entry point — gracefully degrades when no API key is set."""
        if not self._api_key:
            if not self._warned_no_key:
                _log.warning("core_api_disabled", reason="CORE_API_KEY not set")
                self._warned_no_key = True
            return []
        try:
            return await self._search_with_retry(query, max_results, client)
        except RetryError:
            _log.warning("core_retry_exhausted", query=query)
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def _search_with_retry(
        self, query: str, max_results: int, client: httpx.AsyncClient
    ) -> list[Paper]:
        params: dict[str, str | int] = {
            "q": query,
            "limit": min(max_results, 100),
        }
        try:
            resp = await client.get(
                _CORE_BASE,
                params=params,
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            _log.warning("core_error", status=status_code, query=query)
            if status_code in (429,) or status_code >= 500:
                raise
            return []

        data = _safe_json(resp, source="core", query=query)
        if data is None:
            return []
        raw_results = data.get("results")
        results = raw_results if isinstance(raw_results, list) else []
        papers: list[Paper] = []
        now = datetime.now(tz=UTC)
        for item in results:
            if not isinstance(item, dict):
                continue
            doi = (item.get("doi") or "").strip()
            core_id = str(item.get("id") or "").strip()
            external_id = doi or core_id
            if not external_id:
                continue

            title = (item.get("title") or "").strip()
            if not title:
                continue

            # CORE's `authors` is a list of {"name": "Last, First"} objects.
            authors: list[str] = []
            for a in item.get("authors") or []:
                name = (a.get("name") or "").strip() if isinstance(a, dict) else str(a).strip()
                if name:
                    authors.append(name)

            year = item.get("yearPublished")
            if isinstance(year, str) and year.isdigit():
                year = int(year)
            elif not isinstance(year, int):
                year = None

            abstract = item.get("abstract") or None
            # CORE returns `downloadUrl` for the OA PDF when one is hosted by
            # the indexed repository. This is the openAccessPdf-equivalent.
            pdf_url = _sanitise_pdf_url(item.get("downloadUrl") or None)

            papers.append(
                Paper(
                    id=uuid4(),
                    project_id=None,
                    source="core",
                    external_id=external_id,
                    title=title,
                    authors=authors,
                    year=year,
                    abstract=abstract,
                    pdf_url=pdf_url,  # type: ignore[arg-type]
                    citation_key="",  # generated by Librarian after dedup
                    citation_count=item.get("citationCount"),
                    approved=False,
                    added_at=now,
                )
            )

        _log.info("core_results", query=query, count=len(papers))
        return papers


# ---------------------------------------------------------------------------
# Europe PMC adapter — ebi.ac.uk/europepmc
# ---------------------------------------------------------------------------

_EUROPE_PMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


class EuropePMCAdapter:
    """Queries the Europe PMC REST API — life-sciences / biomedical focus.

    No API key required; identifies itself via a User-Agent header (their
    docs recommend it for the "polite" pool).
    """

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "User-Agent": "ResearchFlowAI/0.1 (https://github.com/researchflow-ai)",
            "Accept": "application/json",
        }

    async def search(self, query: str, max_results: int, client: httpx.AsyncClient) -> list[Paper]:
        """Public entry point — wraps the retried inner call, returns [] on exhaustion."""
        try:
            return await self._search_with_retry(query, max_results, client)
        except RetryError:
            _log.warning("europe_pmc_retry_exhausted", query=query)
            return []

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def _search_with_retry(
        self, query: str, max_results: int, client: httpx.AsyncClient
    ) -> list[Paper]:
        params: dict[str, str | int] = {
            "query": query,
            "resultType": "lite",
            "format": "json",
            "pageSize": min(max_results, 100),
        }
        try:
            resp = await client.get(
                _EUROPE_PMC_BASE,
                params=params,
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            _log.warning("europe_pmc_error", status=status_code, query=query)
            if status_code in (429,) or status_code >= 500:
                raise
            return []

        data = _safe_json(resp, source="europe_pmc", query=query)
        if data is None:
            return []
        result_list = data.get("resultList") if isinstance(data.get("resultList"), dict) else {}
        raw_items = result_list.get("result") if isinstance(result_list, dict) else None
        items = raw_items if isinstance(raw_items, list) else []
        papers: list[Paper] = []
        now = datetime.now(tz=UTC)
        for item in items:
            doi = (item.get("doi") or "").strip()
            pmid = (item.get("pmid") or "").strip()
            pmcid = (item.get("pmcid") or "").strip()
            external_id = doi or pmcid or pmid
            if not external_id:
                continue

            title = (item.get("title") or "").strip().rstrip(".")
            if not title:
                continue

            # Europe PMC's `authorString` is a comma-separated string.
            author_str = (item.get("authorString") or "").strip()
            authors = [a.strip() for a in author_str.split(",") if a.strip()] if author_str else []

            year_raw = item.get("pubYear")
            year: int | None = None
            if isinstance(year_raw, int):
                year = year_raw
            elif isinstance(year_raw, str) and year_raw.isdigit():
                year = int(year_raw)

            abstract = item.get("abstractText") or None
            # When the OA full text is in PMC we can synthesise the PDF URL.
            pdf_url: str | None = None
            if pmcid:
                # PMCID arrives as "PMC1234567" — strip the prefix for the URL.
                pmc_num = pmcid.removeprefix("PMC")
                pdf_url = f"https://europepmc.org/backend/ptpmcrender.fcgi?accid=PMC{pmc_num}&blobtype=pdf"

            papers.append(
                Paper(
                    id=uuid4(),
                    project_id=None,
                    source="europe_pmc",
                    external_id=external_id,
                    title=title,
                    authors=authors,
                    year=year,
                    abstract=abstract,
                    pdf_url=pdf_url,  # type: ignore[arg-type]
                    citation_key="",  # generated by Librarian after dedup
                    citation_count=item.get("citedByCount"),
                    approved=False,
                    added_at=now,
                )
            )

        _log.info("europe_pmc_results", query=query, count=len(papers))
        return papers


# ---------------------------------------------------------------------------
# (DiscoveryService moved to app/services/discovery_router.py — see that file
# for the multi-source fan-out, rate limiting, and per-source error policy.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Citation-key helpers (also used by Librarian)
# ---------------------------------------------------------------------------


def _normalise_author(name: str) -> str:
    """Return lowercase alphanumeric last-name fragment from a full author name.

    Handles both 'Last, First' (BibTeX style) and 'First Last' formats.
    """
    name = name.strip()
    if "," in name:
        # 'Smith, Jane' or 'O\'Brien, Alice' — take the part before the comma.
        last = name.split(",")[0].strip()
    else:
        # 'Jane Smith' — take the last word.
        parts = name.split()
        last = parts[-1] if parts else "unknown"
    return re.sub(r"[^a-z0-9]", "", last.lower())


def generate_citation_keys(papers: list[Paper]) -> list[Paper]:
    """Assign unique BibTeX citation keys to a list of papers (in-place copy).

    Format: ``firstauthorlastnameYEAR`` with ``a``, ``b``, ... disambiguation.
    Returns new Paper objects (Pydantic models are immutable by default).
    """
    seen: dict[str, int] = {}
    updated: list[Paper] = []
    for paper in papers:
        author = paper.authors[0] if paper.authors else "unknown"
        year = str(paper.year) if paper.year else "nd"
        base_key = f"{_normalise_author(author)}{year}"

        count = seen.get(base_key, 0)
        seen[base_key] = count + 1
        if count == 0:
            suffix = ""
        elif count <= 26:
            suffix = chr(ord("a") + count - 1)
        else:
            suffix = f"-{count}"
        key = f"{base_key}{suffix}"

        updated.append(paper.model_copy(update={"citation_key": key}))

    return updated
