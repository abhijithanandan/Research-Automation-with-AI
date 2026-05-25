"""Unpaywall enricher — find a legal open-access PDF for each approved paper.

The Critic's full-text RAG (BRD FR-1.2 / :mod:`app.services.fulltext_fetcher`)
only fires when a :class:`Paper` already has a ``pdf_url``. Several of our
sources (notably Crossref) return metadata but **no PDF link** even when an
open-access copy exists *somewhere* on the legal open web — author preprint,
institutional repository, PubMed Central mirror, etc.

`Unpaywall <https://unpaywall.org>`_ is a free service run by the OurResearch
nonprofit that maps DOIs to those OA URLs. It's the standard tool used by
Zotero and reputable lit-review platforms for this exact problem.

This module:

  1. Walks the approved-paper pool.
  2. For every paper without a ``pdf_url`` whose ``external_id`` looks like a
     DOI, queries ``https://api.unpaywall.org/v2/<doi>?email=<contact>``.
  3. If a ``best_oa_location.url_for_pdf`` is returned, mutates the paper to
     carry that URL — :class:`FullTextFetcher` then picks it up downstream.

Behaviour notes (BRD-compliant by construction):

* Unpaywall *only* indexes legitimate OA copies. Every URL it returns comes
  with a license + host metadata; we don't visit publisher paywalls.
* Without ``settings.unpaywall_email`` the enricher is a no-op — Unpaywall's
  ToS requires every request to identify the caller, and we refuse to send
  anonymous traffic.
* A single API failure is non-fatal — the paper keeps its empty ``pdf_url``
  and the Critic falls back to abstract-only extraction (same contract as
  the rest of the Phase 2 pipeline).
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import quote

import httpx

from app.config import get_settings
from app.models.schemas import Paper
from app.utils.logging import get_logger

_log = get_logger(__name__)

_UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
# Unpaywall is gentle but their docs ask for "courteous" pacing.
_PER_REQUEST_DELAY_S = 0.1
_HTTP_TIMEOUT = httpx.Timeout(20.0)
# A DOI looks like "10.<registrant>/<suffix>" — RFC-adjacent regex from
# the Crossref docs (intentionally permissive on the suffix).
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


class UnpaywallEnricher:
    """Resolve OA PDF URLs for papers that lack one.

    The ``email`` argument is the contact identifier sent with every request
    per Unpaywall's ToS. Without it the enricher refuses to make network
    calls — anonymous traffic is not allowed.
    """

    def __init__(self, email: str = "") -> None:
        self._email = email.strip()
        self._warned_no_email = False

    async def enrich(self, papers: list[Paper]) -> list[Paper]:
        """Return a new list of papers with ``pdf_url`` populated where possible.

        Papers that already have a ``pdf_url`` pass through unchanged. Papers
        whose ``external_id`` is not a DOI are also passed through — Unpaywall
        only indexes by DOI. Failures are logged and the paper is returned
        unchanged.
        """
        if not papers:
            return []
        if not self._email:
            if not self._warned_no_email:
                _log.warning(
                    "unpaywall_disabled",
                    reason="UNPAYWALL_EMAIL not set; skipping OA enrichment",
                )
                self._warned_no_email = True
            return papers

        enriched: list[Paper] = []
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": "ResearchFlowAI/0.1"},
        ) as client:
            for i, paper in enumerate(papers):
                # Per-paper try/except — a single bad DOI must not drain the
                # batch. Before this guard, an unexpected error mid-loop lost
                # every paper that came after it (audit finding #2).
                try:
                    if paper.pdf_url is not None:
                        # Already has an OA PDF — nothing to do.
                        enriched.append(paper)
                        continue

                    doi = self._extract_doi(paper)
                    if doi is None:
                        enriched.append(paper)
                        continue

                    if i > 0:
                        await asyncio.sleep(_PER_REQUEST_DELAY_S)

                    oa_url = await self._lookup_pdf_url(client, doi, paper.citation_key)
                    if oa_url is None:
                        enriched.append(paper)
                        continue

                    # Pydantic models are immutable; build a copy with the URL filled in.
                    enriched.append(paper.model_copy(update={"pdf_url": oa_url}))
                    _log.info(
                        "unpaywall_pdf_resolved",
                        citation_key=paper.citation_key,
                        doi=doi,
                    )
                except Exception as exc:
                    _log.warning(
                        "unpaywall_per_paper_error",
                        citation_key=paper.citation_key,
                        error=str(exc),
                    )
                    enriched.append(paper)

        return enriched

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_doi(paper: Paper) -> str | None:
        """Return the paper's DOI if ``external_id`` looks like one, else None.

        Some sources (Semantic Scholar, Crossref) already give us a DOI as the
        external id. Others (arXiv, Europe PMC) don't — those are skipped.
        """
        ext_id = (paper.external_id or "").strip()
        if not ext_id:
            return None
        # Strip a "doi:" prefix if present.
        ext_id = ext_id.removeprefix("doi:").removeprefix("DOI:")
        if _DOI_RE.match(ext_id):
            return ext_id
        return None

    async def _lookup_pdf_url(
        self, client: httpx.AsyncClient, doi: str, citation_key: str
    ) -> str | None:
        """Query Unpaywall for one DOI; return the best OA PDF URL or None."""
        url = f"{_UNPAYWALL_BASE}/{quote(doi, safe='/')}"
        try:
            resp = await client.get(url, params={"email": self._email})
            if resp.status_code == 404:
                # Not found in Unpaywall's index — totally normal for non-OA work.
                return None
            resp.raise_for_status()
            # Defensive: a 200 with non-JSON body (HTML error page, truncated
            # response) used to raise an uncaught JSONDecodeError that
            # propagated out of enrich() — audit finding #1.
            data = resp.json()
        except httpx.HTTPError as exc:
            _log.warning(
                "unpaywall_lookup_failed",
                citation_key=citation_key,
                doi=doi,
                error=str(exc),
            )
            return None
        except (ValueError, TypeError) as exc:  # JSONDecodeError is a ValueError
            _log.warning(
                "unpaywall_lookup_bad_json",
                citation_key=citation_key,
                doi=doi,
                error=str(exc),
            )
            return None

        if not isinstance(data, dict):
            return None
        if not data.get("is_oa"):
            return None
        best = data.get("best_oa_location")
        if not isinstance(best, dict):
            return None
        pdf_url = best.get("url_for_pdf")
        if isinstance(pdf_url, str) and pdf_url.startswith(("http://", "https://")):
            return pdf_url
        return None


# Module-level singleton — matches the other service patterns.
_enricher: UnpaywallEnricher | None = None


def get_unpaywall_enricher() -> UnpaywallEnricher:
    """Return the module-level enricher, creating it on first call."""
    global _enricher
    if _enricher is None:
        _enricher = UnpaywallEnricher(email=get_settings().unpaywall_email)
    return _enricher
