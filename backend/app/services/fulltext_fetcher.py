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
from uuid import UUID

import httpx
from pypdf import PdfReader

from app.models.schemas import Paper
from app.services.vector_store import VectorStore, VectorStoreUnavailableError
from app.utils.logging import get_logger

_log = get_logger(__name__)

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
# Spacing between PDF downloads from the same host so we stay polite.
_PER_HOST_DELAY_S = 1.0
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

    async def ingest(self, project_id: UUID, papers: list[Paper]) -> int:
        """Download → parse → chunk → embed every fetchable paper.

        Returns the count of papers successfully ingested. Failures are logged
        as warnings and skipped — the Critic falls back to abstract-only
        extraction for papers we couldn't fetch (this is the same graceful-
        degradation contract as :class:`app.services.vector_store`).
        """
        if not papers:
            return 0

        ingested = 0
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(_DOWNLOAD_TIMEOUT_S),
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            for i, paper in enumerate(papers):
                if i > 0:
                    await asyncio.sleep(_PER_HOST_DELAY_S)
                url = self._resolve_pdf_url(paper)
                if url is None:
                    _log.info(
                        "fulltext_no_pdf_url",
                        citation_key=paper.citation_key,
                        source=paper.source,
                    )
                    continue

                pdf_bytes = await self._download_pdf(client, url, paper.citation_key)
                if pdf_bytes is None:
                    continue

                text = await asyncio.to_thread(self._extract_text, pdf_bytes, paper.citation_key)
                if not text:
                    continue

                chunks = self._chunk(text)
                if not chunks:
                    continue

                try:
                    await self._embed_chunks(project_id, paper.citation_key, chunks)
                    ingested += 1
                    _log.info(
                        "fulltext_ingested",
                        citation_key=paper.citation_key,
                        chunks=len(chunks),
                        chars=len(text),
                    )
                except VectorStoreUnavailableError as exc:
                    # If the vector store is down we just stop — the Critic
                    # has its own VectorStoreUnavailable handling.
                    _log.warning("fulltext_embed_failed", error=str(exc))
                    return ingested

        return ingested

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_pdf_url(paper: Paper) -> str | None:
        """Return the legitimate PDF URL for this paper, or None.

        Only ``arxiv`` and ``semantic_scholar`` sources expose a known PDF
        endpoint. Crossref-only papers reach us with metadata but no direct
        OA PDF link — they're skipped (the BRD path for those is user upload
        via the local client, not backend scraping).
        """
        pdf = paper.pdf_url
        if pdf is None:
            return None
        url = str(pdf)
        # Trust only the OA mirrors we know about. This is a deliberate allow-
        # list — keeps us from accidentally hitting publisher hosts whose ToS
        # forbids automated access.
        if "arxiv.org" in url or "semanticscholar.org" in url:
            return url
        # Some Semantic Scholar `openAccessPdf.url` values point at the
        # publisher's own OA mirror (e.g. aclanthology.org, biorxiv.org,
        # plos.org). Those publishers operate the OA mirror specifically to
        # be downloaded — treat them as OK.
        oa_hosts = (
            "aclanthology.org",
            "biorxiv.org",
            "medrxiv.org",
            "plos.org",
            "nature.com/articles",  # OA Nature articles only — path-checked
            "frontiersin.org",
            "mdpi.com",
            "openreview.net",
            "ncbi.nlm.nih.gov/pmc",  # PMC = PubMed Central, OA archive
        )
        if any(host in url for host in oa_hosts):
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
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            _log.warning(
                "fulltext_download_failed",
                citation_key=citation_key,
                url=url,
                error=str(exc),
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
        # Cheap sniff — PDFs start with the literal "%PDF" header. If the URL
        # redirected to an HTML interstitial we bail rather than embed garbage.
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
            _log.warning("fulltext_pdf_parse_failed", citation_key=citation_key, error=str(exc))
            return ""
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                # Some malformed pages throw inside pypdf — skip them.
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
