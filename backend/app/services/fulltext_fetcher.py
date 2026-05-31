"""Full-text PDF ingestion service — BRD FR-1.2 (Local Document Parser).

The Critic's RAG quality jumps significantly when it has full-text instead of
just abstracts. This service:

  1. Takes the approved-paper pool.
  2. For each paper that exposes an open-access PDF URL (Semantic Scholar's
     ``openAccessPdf.url`` or arXiv's direct ``/pdf/<id>``), downloads the PDF
     via httpx — these URLs were *given to us by the source APIs* as the
     legitimate download endpoints. No scraping, no anti-bot bypass.
  3. Extracts text with ``pypdf`` (page-by-page).
  4. Chunks the text paragraph-wise (with a hard length cap for runaway pages).
  5. Upserts the chunks into the project's ChromaDB namespace so the Critic's
     ``vector_store.query`` calls surface real paper content instead of just
     the abstract.

Out of scope here (deliberately):

* Anti-bot bypass / Cloudflare/Turnstile/reCAPTCHA evasion.
* Scraping sites whose ToS forbids automated access (Google Scholar, IEEE
  Xplore, ResearchGate, etc.).
* Fetching paywalled content. The BRD architecture says non-OA PDFs are
  uploaded by the user via the local client (FR-1.2 / FR-1.3).
"""

from __future__ import annotations

import asyncio
import io
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse
from uuid import UUID

import httpx
from pypdf import PdfReader

from app.models.schemas import Paper
from app.services.vector_store import VectorStore, VectorStoreUnavailableError
from app.utils.logging import get_logger

_log = get_logger(__name__)

# CodeRabbit / OA-host allow-list (W3-Auth follow-up): substring matching of
# "arxiv.org" in url is unsafe — https://evil.com/?fake=arxiv.org passes.
# Match on urlparse(...).netloc with strict suffix rules instead, and disable
# httpx follow_redirects so a redirect can't escape the allow-list. Each
# (host, [optional path-prefix]) pair below names one trusted OA mirror.
_OA_HOSTS_ALLOWLIST: tuple[tuple[str, str | None], ...] = (
    ("arxiv.org", "/"),
    ("www.arxiv.org", "/"),
    ("export.arxiv.org", "/"),
    ("semanticscholar.org", "/"),
    ("www.semanticscholar.org", "/"),
    ("aclanthology.org", "/"),
    ("www.aclanthology.org", "/"),
    ("biorxiv.org", "/"),
    ("www.biorxiv.org", "/"),
    ("medrxiv.org", "/"),
    ("www.medrxiv.org", "/"),
    ("plos.org", "/"),
    ("journals.plos.org", "/"),
    # OA Nature articles only — restrict by path.
    ("nature.com", "/articles"),
    ("www.nature.com", "/articles"),
    ("frontiersin.org", "/"),
    ("www.frontiersin.org", "/"),
    ("mdpi.com", "/"),
    ("www.mdpi.com", "/"),
    ("openreview.net", "/"),
    # PubMed Central — only the PMC archive prefix.
    ("ncbi.nlm.nih.gov", "/pmc"),
    ("www.ncbi.nlm.nih.gov", "/pmc"),
)


def _is_allowed_pdf_url(url: str) -> bool:
    """Return True iff `url` matches the OA allow-list under strict host+path
    rules. Rejects javascript:/file:/ftp:/data:, IP literals, IDN punycode
    spoofs (we just compare netloc as-is — caller normalises if needed)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    path = parsed.path or "/"
    for allowed_host, path_prefix in _OA_HOSTS_ALLOWLIST:
        if host == allowed_host and (path_prefix is None or path.startswith(path_prefix)):
            return True
    return False


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Stay under Chroma's default per-document size and the embedding model's
# context. ~2 000 chars ≈ 500 tokens, well below the 512-token sweet spot for
# all-MiniLM-L6-v2 (Chroma's default embedder).
_MAX_CHUNK_CHARS = 1800
# Drop chunks below this — usually page numbers or headers, not useful context.
_MIN_CHUNK_CHARS = 120
# Skip absurdly large PDFs to avoid pulling 200-page theses into memory.
_MAX_PDF_BYTES = 20 * 1024 * 1024  # 20 MB
# Per-PDF download timeout. Slow OA mirrors do exist.
_DOWNLOAD_TIMEOUT_S = 30.0
# W2-C1: cap concurrent PDF downloads. 5 is below the politeness threshold
# of every public OA mirror we touch (most allow ~10 concurrent), keeps the
# host's connection pool small, and still parallelises ~30 papers down from
# ~120s sequential to ~30s wall-clock.
_CONCURRENT_DOWNLOADS = 5
# User-Agent — honest identification (BRD FR-1.3 spirit: never headless without
# consent → here we identify ourselves clearly on every request).
_USER_AGENT = "ResearchFlowAI/0.1 (https://github.com/researchflow-ai)"

# Strip C0/C1 control characters except newline (\n) and tab (\t). pypdf
# occasionally emits NUL and other unprintables that break Chroma's
# document-text validation and corrupt LLM prompts.
_CTRL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class FullTextFetcher:
    """Fetch and embed open-access full-text PDFs for the approved pool."""

    def __init__(self, vector_store: VectorStore) -> None:
        self._vs = vector_store

    async def ingest(
        self,
        project_id: UUID,
        papers: list[Paper],
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> int:
        """Download → parse → chunk → embed every fetchable paper.

        Returns the count of papers successfully ingested. Failures are logged
        as warnings and skipped — the Critic falls back to abstract-only
        extraction for papers we couldn't fetch (this is the same graceful-
        degradation contract as :class:`app.services.vector_store`).

        W2-C1: downloads run concurrently under a Semaphore so a typical
        ~30-paper pool ingests in ~30s instead of ~120s. ``on_progress`` (if
        provided) is awaited after each paper's pipeline completes — workflow
        callers pass a closure that emits a WS ``fulltext_progress`` event so
        the frontend can show a "N/M papers indexed" chip during what used to
        be a silent 2-minute wait.
        """
        if not papers:
            return 0

        total = len(papers)
        done = 0
        ingested = 0
        # Lock guards the (done, ingested) counter increments + the
        # on_progress dispatch so concurrent finishes can't race to emit
        # progress events out of order.
        progress_lock = asyncio.Lock()
        sem = asyncio.Semaphore(_CONCURRENT_DOWNLOADS)

        async with httpx.AsyncClient(
            # CodeRabbit / W3-Auth: do NOT follow redirects automatically —
            # a permissive 3xx Location could escape the OA allow-list.
            # _download_pdf walks the redirect chain manually and revalidates
            # each hop against _is_allowed_pdf_url.
            follow_redirects=False,
            timeout=httpx.Timeout(_DOWNLOAD_TIMEOUT_S),
            headers={"User-Agent": _USER_AGENT},
        ) as client:

            async def _run_one(paper: Paper) -> None:
                # CodeRabbit: progress must NEVER stall even if _ingest_one
                # raises an unexpected exception. Wrap the per-paper call in
                # try/except so the finally block always increments done/
                # ingested and emits the progress event. asyncio.gather is
                # called with return_exceptions=True too, but a raise here
                # would short-circuit BEFORE the finally — explicit finally
                # is what guarantees the counter advances.
                nonlocal done, ingested
                ok = False
                try:
                    async with sem:
                        ok = await self._ingest_one(client, project_id, paper)
                except Exception as exc:
                    _log.warning(
                        "fulltext_ingest_one_raised",
                        citation_key=paper.citation_key,
                        error_type=type(exc).__name__,
                    )
                    ok = False
                finally:
                    async with progress_lock:
                        done += 1
                        if ok:
                            ingested += 1
                        if on_progress is not None:
                            try:
                                await on_progress(done, total)
                            except Exception as exc:  # progress is observational
                                _log.warning(
                                    "fulltext_progress_emit_failed",
                                    error_type=type(exc).__name__,
                                )

            # return_exceptions=True so a single failing task can't bubble up
            # and abort the gather — _ingest_one already converts any per-paper
            # failure into a returned False + warning log.
            await asyncio.gather(*(_run_one(p) for p in papers), return_exceptions=True)

        return ingested

    async def _ingest_one(
        self,
        client: httpx.AsyncClient,
        project_id: UUID,
        paper: Paper,
    ) -> bool:
        """Pipeline for one paper. Returns True if a chunk made it into the
        vector store. Any failure is logged and returns False — the caller
        treats False as "this paper didn't ingest, move on."
        """
        url = self._resolve_pdf_url(paper)
        if url is None:
            _log.info(
                "fulltext_no_pdf_url",
                citation_key=paper.citation_key,
                source=paper.source,
            )
            return False

        pdf_bytes = await self._download_pdf(client, url, paper.citation_key)
        if pdf_bytes is None:
            return False

        text = await asyncio.to_thread(self._extract_text, pdf_bytes, paper.citation_key)
        if not text:
            return False

        chunks = self._chunk(text)
        if not chunks:
            return False

        try:
            await self._embed_chunks(project_id, paper.citation_key, chunks)
            _log.info(
                "fulltext_ingested",
                citation_key=paper.citation_key,
                chunks=len(chunks),
                chars=len(text),
            )
            return True
        except VectorStoreUnavailableError as exc:
            _log.warning("fulltext_embed_failed", error_type=type(exc).__name__, error=str(exc))
            # Note: the caller's gather() will see this as a False return; the
            # caller doesn't currently propagate this to a hard stop, which
            # matches the prior contract (best-effort ingest, Critic falls back).
            return False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_pdf_url(paper: Paper) -> str | None:
        """Return the legitimate PDF URL for this paper, or None.

        Uses :func:`_is_allowed_pdf_url` — strict netloc+path matching against
        the OA allow-list. Substring matching (the prior approach) would let
        ``https://evil.com/?fake=arxiv.org`` pass. CodeRabbit / W3-Auth fix.
        """
        pdf = paper.pdf_url
        if pdf is None:
            return None
        url = str(pdf)
        if _is_allowed_pdf_url(url):
            return url
        # Unknown host — log and skip. We don't try to be clever here.
        _log.info(
            "fulltext_skipping_unknown_host",
            citation_key=paper.citation_key,
            url=url,
        )
        return None

    async def _download_pdf(
        self, client: httpx.AsyncClient, url: str, citation_key: str
    ) -> bytes | None:
        # Manual redirect chain so every hop revalidates against the OA
        # allow-list (CodeRabbit / W3-Auth). httpx is configured with
        # follow_redirects=False; we walk up to 5 hops.
        current = url
        try:
            for _ in range(5):
                resp = await client.get(current)
                if resp.status_code in (301, 302, 303, 307, 308):
                    next_url = resp.headers.get("Location")
                    if not next_url:
                        _log.info(
                            "fulltext_redirect_no_location",
                            citation_key=citation_key,
                            status=resp.status_code,
                        )
                        return None
                    # Resolve relative redirects against the current URL.
                    next_abs = str(httpx.URL(current).join(next_url))
                    if not _is_allowed_pdf_url(next_abs):
                        _log.warning(
                            "fulltext_redirect_outside_allowlist",
                            citation_key=citation_key,
                            from_url=current,
                            to_url=next_abs,
                        )
                        return None
                    current = next_abs
                    continue
                resp.raise_for_status()
                break
            else:
                # for/else: 5 redirects without a terminal response.
                _log.warning("fulltext_redirect_loop", citation_key=citation_key)
                return None
        except httpx.HTTPError as exc:
            _log.warning(
                "fulltext_download_failed",
                citation_key=citation_key,
                url=current,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

        # M3-B: declared content-type filter. Some publishers serve a 200
        # OK with a paywall HTML interstitial when the user isn't
        # authenticated — Content-Type=text/html. Bail before reading the
        # body so we don't waste bytes on something we can't parse.
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype and not (
            ctype.startswith("application/pdf") or ctype.startswith("application/octet-stream")
        ):
            _log.info(
                "fulltext_wrong_content_type",
                citation_key=citation_key,
                content_type=ctype.split(";", 1)[0],
            )
            return None

        content = resp.content
        if not content:
            return None
        if len(content) > _MAX_PDF_BYTES:
            _log.warning(
                "fulltext_pdf_too_large",
                citation_key=citation_key,
                bytes=len(content),
            )
            return None
        # Cheap sniff — PDFs start with the literal "%PDF" header. Even if
        # the server lied about content-type, this still catches non-PDF
        # bodies before they reach pypdf. Defense in depth alongside the
        # content-type filter above.
        if not content.startswith(b"%PDF"):
            _log.info(
                "fulltext_not_a_pdf",
                citation_key=citation_key,
                head=content[:64].decode("latin-1", errors="replace"),
            )
            return None
        return content

    @staticmethod
    def _extract_text(pdf_bytes: bytes, citation_key: str) -> str:
        """Parse the PDF synchronously — called from a thread pool."""
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
        except Exception as exc:
            _log.warning(
                "fulltext_pdf_parse_failed",
                citation_key=citation_key,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return ""
        pages: list[str] = []
        for page_idx, page in enumerate(reader.pages):
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:
                # Wave-3/A3: log the exception class so a real recurring
                # pypdf bug is visible in ops logs. Per-page graceful
                # degradation contract is intentional — one malformed page
                # never sinks the whole PDF.
                _log.debug(
                    "fulltext_page_extract_failed",
                    citation_key=citation_key,
                    page=page_idx,
                    error_type=type(exc).__name__,
                )
                continue
        # Re-join pages with a paragraph break so the chunker sees natural
        # section boundaries.
        joined = "\n\n".join(p.strip() for p in pages if p.strip())
        # Sanitise: pypdf occasionally surfaces NULs and other control chars
        # from broken font tables. Chroma rejects NUL in document text and
        # Gemini sometimes fails to encode them. Keep newlines and tabs; drop
        # everything else in the C0/C1 range (audit finding #8).
        return _CTRL_CHAR_RE.sub("", joined)

    @staticmethod
    def _chunk(text: str) -> list[str]:
        """Paragraph-based chunking with a hard length cap.

        For research papers, paragraph boundaries align with semantic units
        (rag-architect's Chunking Strategy Matrix). Long paragraphs are hard-
        sliced at sentence boundaries to stay under ``_MAX_CHUNK_CHARS``.
        """
        # Normalise whitespace so a long PDF with weird line wrapping doesn't
        # produce thousands of single-word "paragraphs".
        text = re.sub(r"[ \t]+", " ", text)
        raw_paras = [p.strip() for p in re.split(r"\n\s*\n+", text)]
        chunks: list[str] = []
        for para in raw_paras:
            if len(para) < _MIN_CHUNK_CHARS:
                continue
            if len(para) <= _MAX_CHUNK_CHARS:
                chunks.append(para)
                continue
            # Long paragraph → split on sentence boundaries, greedily packing
            # into <= _MAX_CHUNK_CHARS pieces.
            sentences = re.split(r"(?<=[.!?])\s+", para)
            current = ""
            for s in sentences:
                # CodeRabbit: a single sentence longer than the cap (rare —
                # equation-heavy run-ons, or PDFs without sentence terminators)
                # used to be assigned to `current` whole, producing an
                # oversized chunk. Hard-slice such sentences into substrings
                # of at most _MAX_CHUNK_CHARS so the cap holds even in the
                # pathological case.
                if len(s) > _MAX_CHUNK_CHARS:
                    if len(current) >= _MIN_CHUNK_CHARS:
                        chunks.append(current)
                    current = ""
                    for start in range(0, len(s), _MAX_CHUNK_CHARS):
                        piece = s[start : start + _MAX_CHUNK_CHARS]
                        if len(piece) >= _MIN_CHUNK_CHARS:
                            chunks.append(piece)
                    continue
                if len(current) + len(s) + 1 <= _MAX_CHUNK_CHARS:
                    current = f"{current} {s}".strip()
                else:
                    if len(current) >= _MIN_CHUNK_CHARS:
                        chunks.append(current)
                    current = s
            if len(current) >= _MIN_CHUNK_CHARS:
                chunks.append(current)
        return chunks

    async def _embed_chunks(self, project_id: UUID, citation_key: str, chunks: list[str]) -> None:
        """Upsert chunks into the project's ChromaDB namespace.

        Each chunk gets an id of the form ``<citation_key>:<index>`` so a re-
        ingest replaces (not duplicates) — matches Chroma's upsert semantics.
        """
        documents: list[dict[str, object]] = [
            {"id": f"{citation_key}:{i}", "text": chunk} for i, chunk in enumerate(chunks)
        ]
        await self._vs.upsert(namespace=str(project_id), documents=documents)


# Module-level singleton — same pattern as the LLM gateway / vector store.
_fetcher: FullTextFetcher | None = None


def get_fulltext_fetcher() -> FullTextFetcher:
    """Return the module-level fetcher, creating it on first call."""
    from app.services.vector_store import get_vector_store

    global _fetcher
    if _fetcher is None:
        _fetcher = FullTextFetcher(vector_store=get_vector_store())
    return _fetcher
