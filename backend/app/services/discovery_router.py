"""Multi-source discovery router — fans out the Librarian's expanded queries
across every external paper-metadata API the project supports.

Sources wired (all free, all ToS-compliant):

  - **Semantic Scholar** — broad, citation-rich (optional API key raises limits)
  - **arXiv** — preprints, no key
  - **Crossref** — DOI registry, ~140M records, no key
  - **CORE** — global OA aggregator, ~280M works (requires free API key)
  - **Europe PMC** — biomedical / life sciences, no key

The five sources run **concurrently** with each other; the queries *within* one
source run **sequentially with a per-source delay** so each provider's rate
limit is respected. A failure in any single source is non-fatal — the merge
continues with whatever returned successfully (BRD §10 mitigation:
"graceful degradation if one API goes down").

Adapters themselves live in :mod:`app.services.discovery` — this file owns
only the routing, scheduling, and merge-into-pool concerns.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import httpx

from app.config import get_settings
from app.models.schemas import Paper
from app.services.discovery import (
    ArXivAdapter,
    CoreAdapter,
    CrossrefAdapter,
    EuropePMCAdapter,
    SemanticScholarAdapter,
    SourceAdapter,
    SourceUnavailableError,
)
from app.utils.logging import get_logger

_log = get_logger(__name__)


class DiscoveryService:
    """Aggregates results from five free academic APIs in parallel.

    Each adapter is independently rate-limited; one failing source does not
    sink the run (BRD §10 risk mitigation). The output is the union of every
    successful adapter result — citation-key generation and Levenshtein/DOI
    deduplication happen one step later in the Librarian agent.
    """

    # Minimum spacing between consecutive requests to the *same* source.
    # Tuned per provider's published / observed rate limits:
    #   arXiv         — official guidance: ~1 req / 3 s
    #   Semantic Sch. — 1 req/s with API key (the free tier is harsher)
    #   CORE          — free tier 10 req/min ≈ 6 s; 6.5 s leaves slack
    #   Crossref      — gentle; the polite-pool spec recommends courteous pacing
    #   Europe PMC    — gentle; 1 s is generous
    _PER_SOURCE_DELAY_S: ClassVar[dict[type, float]] = {
        ArXivAdapter: 3.0,
        SemanticScholarAdapter: 1.0,
        CoreAdapter: 6.5,
        CrossrefAdapter: 0.5,
        EuropePMCAdapter: 1.0,
    }

    def __init__(self) -> None:
        settings = get_settings()
        self._adapters: list[SourceAdapter] = [
            SemanticScholarAdapter(api_key=settings.semantic_scholar_api_key),
            ArXivAdapter(),
            CrossrefAdapter(mailto=settings.crossref_mailto),
            CoreAdapter(api_key=settings.core_api_key),
            EuropePMCAdapter(),
        ]

    async def search(
        self,
        queries: list[str],
        max_per_source: int = 30,
        arxiv_categories: list[str] | None = None,
    ) -> list[Paper]:
        """Search every source for every query and return the merged list.

        Returns:
            The flat union of papers returned across all sources. Order is not
            stable. Dedup happens in :func:`app.agents.librarian.Librarian`.
        """
        limits = httpx.Limits(max_connections=25, max_keepalive_connections=8)
        async with httpx.AsyncClient(follow_redirects=True, limits=limits) as client:
            # One task per source — each drains all queries sequentially.
            source_tasks = [
                self._run_source(adapter, queries, max_per_source, arxiv_categories, client)
                for adapter in self._adapters
            ]
            per_source = await asyncio.gather(*source_tasks, return_exceptions=True)

        papers: list[Paper] = []
        for source_result in per_source:
            if isinstance(source_result, BaseException):
                _log.warning("discovery_source_error", error=str(source_result))
                continue
            papers.extend(source_result)
        return papers

    async def _run_source(
        self,
        adapter: SourceAdapter,
        queries: list[str],
        max_per_source: int,
        arxiv_categories: list[str] | None,
        client: httpx.AsyncClient,
    ) -> list[Paper]:
        """Run every query against one source, sequentially, with rate spacing.

        A single query failing is non-fatal — its papers are simply absent from
        the merge. We log the exception so it is traceable in audit_log.
        """
        delay = self._PER_SOURCE_DELAY_S.get(type(adapter), 0.5)
        papers: list[Paper] = []
        # Fail-fast: a source that errors for the first 2 queries will keep
        # doing so for the rest of this run. Each retry-exhausted query costs
        # ~60 s (HTTP timeout x 3 attempts) -- five of them blocks the whole
        # graph for minutes. After 2 consecutive FAILURES we skip the rest.
        #
        # CodeRabbit follow-up: only true failures (exception, retry-exhausted
        # -> []) count. A legitimately empty result from an expanded query
        # is fine — later expanded queries may still hit. Previously a single
        # zero-hit query path was conflated with a failure and tripped the
        # short-circuit, missing later queries.
        consecutive_failures = 0
        for i, query in enumerate(queries):
            if i > 0:
                await asyncio.sleep(delay)
            failed = False
            try:
                if isinstance(adapter, ArXivAdapter):
                    result = await adapter.search(
                        query, max_per_source, client, categories=arxiv_categories
                    )
                else:
                    result = await adapter.search(query, max_per_source, client)
            except SourceUnavailableError as exc:
                # CodeRabbit: retry-exhausted is the case fail-fast was built
                # for. Adapters now raise SourceUnavailableError instead of
                # swallowing RetryError -> []; that path now reaches us as a
                # real failure signal and bumps consecutive_failures.
                _log.warning(
                    "discovery_source_unavailable",
                    source=type(adapter).__name__,
                    query=query,
                    error=str(exc),
                )
                result = []
                failed = True
            except Exception as exc:  # one query must not sink the whole source
                _log.warning(
                    "discovery_query_error",
                    source=type(adapter).__name__,
                    query=query,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                result = []
                failed = True
            papers.extend(result)
            consecutive_failures = consecutive_failures + 1 if failed else 0
            if consecutive_failures >= 2 and i < len(queries) - 1:
                _log.warning(
                    "discovery_source_short_circuited",
                    source=type(adapter).__name__,
                    consecutive_failures=consecutive_failures,
                    skipped_queries=len(queries) - i - 1,
                )
                break
        return papers
