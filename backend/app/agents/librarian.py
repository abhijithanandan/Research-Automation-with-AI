"""The Librarian agent — discovery. See SPEC.md §6.1 and docs/agents/librarian.md.

Implements all 6 behaviors from the agent contract:
  1. Query expansion via LLMGateway.
  2. Source fan-out via DiscoveryService (never calls httpx directly).
  3. Deduplication by DOI then fuzzy title (token-set ratio ≥ 0.9).
  4. Ranking by relevance + citation count + recency.
  5. Trimming to max_candidates.
  6. Citation key generation (firstauthorlastnameYEAR, disambiguated with a/b/…).

Every call writes to audit_log before returning (caller must pass audit_writer).
"""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from thefuzz import fuzz

from app.agents.base import Agent
from app.models.schemas import Paper
from app.services.discovery import DiscoveryService, generate_citation_keys
from app.services.llm import get_llm_gateway
from app.utils.logging import get_logger

_log = get_logger(__name__)

_EXPANSION_PROMPT_TEMPLATE = """\
You are a research librarian. Generate {n} alternative search queries for the \
seed query below. Return ONLY a JSON array of strings, no other text.

Seed query: {seed_query}

Example output: ["query one", "query two", "query three"]
"""


class SearchQueries(BaseModel):
    queries: list[str]


class LibrarianInput(BaseModel):
    seed_query: str
    max_candidates: int = 30
    sources: list[Literal["semantic_scholar", "arxiv", "crossref"]] = [
        "semantic_scholar",
        "arxiv",
    ]
    project_id: UUID | None = None  # set by the graph node to stamp Paper rows


class LibrarianOutput(BaseModel):
    candidates: list[Paper]  # approved=False always
    expanded_queries: list[str]


class Librarian(Agent[LibrarianInput, LibrarianOutput]):
    name = "librarian"

    def __init__(self) -> None:
        self._discovery = DiscoveryService()
        self._llm = get_llm_gateway()

    async def run(self, payload: LibrarianInput) -> LibrarianOutput:
        # 1. Query expansion -------------------------------------------------
        expanded_queries = await self._expand_query(payload.seed_query)
        all_queries = [payload.seed_query, *expanded_queries]
        _log.info(
            "librarian_queries",
            seed=payload.seed_query,
            expanded=expanded_queries,
        )

        # 2. Source fan-out ---------------------------------------------------
        raw_candidates = await self._discovery.search(
            queries=all_queries,
            max_per_source=max(payload.max_candidates, 50),
        )

        # 3. Deduplication ----------------------------------------------------
        deduped = _deduplicate(raw_candidates)

        # 4. Ranking ----------------------------------------------------------
        ranked = _rank(deduped)

        # 5. Trim to max_candidates -------------------------------------------
        trimmed = ranked[: payload.max_candidates]

        # 6. Stamp project_id + generate citation keys ------------------------
        if payload.project_id is not None:
            trimmed = [p.model_copy(update={"project_id": payload.project_id}) for p in trimmed]
        trimmed = generate_citation_keys(trimmed)

        # Invariant: every returned paper has approved=False (SPEC §6.1).
        assert all(not p.approved for p in trimmed)

        _log.info("librarian_done", candidate_count=len(trimmed))
        return LibrarianOutput(candidates=trimmed, expanded_queries=expanded_queries)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _expand_query(self, seed_query: str) -> list[str]:
        """Use the LLM to generate 3–5 alternative search queries.

        Falls back to an empty list if the LLM provider is unavailable —
        the Librarian still runs with the seed query alone (see failure modes).
        """
        prompt = _EXPANSION_PROMPT_TEMPLATE.format(n=4, seed_query=seed_query)
        try:
            from google.genai import types as genai_types

            config = genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SearchQueries,
            )
            text, _telemetry = await self._llm.complete(prompt, config=config)
            data = json.loads(text)
            queries = data.get("queries", [])
            return [str(q) for q in queries[:5]]
        except Exception as exc:
            _log.warning("librarian_expansion_failed", error=str(exc))
            return []  # graceful degradation: continue with seed only


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------

_FUZZY_RATIO_THRESHOLD = 90  # token_set_ratio ≥ 90 → treat as duplicate


def _normalise_doi(doi: str | None) -> str:
    if not doi:
        return ""
    return doi.lower().strip().lstrip("https://doi.org/").lstrip("http://dx.doi.org/")


def _deduplicate(papers: list[Paper]) -> list[Paper]:
    """Remove duplicates by DOI first, then by fuzzy title.

    Keeps the first occurrence (prefer Semantic Scholar ordering which tends
    to be more complete). This mirrors the contract in docs/agents/librarian.md.
    """
    seen_dois: set[str] = set()
    unique: list[Paper] = []

    for paper in papers:
        doi = _normalise_doi(paper.external_id if "10." in paper.external_id else None)

        if doi:
            if doi in seen_dois:
                continue
            seen_dois.add(doi)
            unique.append(paper)
            continue

        # No DOI — check fuzzy title against existing unique titles.
        title_normalised = paper.title.lower().strip()
        is_dup = False
        for existing in unique:
            ratio = fuzz.token_set_ratio(title_normalised, existing.title.lower().strip())
            if ratio >= _FUZZY_RATIO_THRESHOLD:
                is_dup = True
                break

        if not is_dup:
            unique.append(paper)

    return unique


# ---------------------------------------------------------------------------
# Ranking helpers
# ---------------------------------------------------------------------------

_CITATION_WEIGHT = 0.5
_RECENCY_WEIGHT = 0.3
_POSITION_WEIGHT = 0.2
_BASE_YEAR = 2000


def _rank(papers: list[Paper]) -> list[Paper]:
    """Score each paper and return sorted descending.

    Score = position_score * W_pos + recency_score * W_rec + citation_score * W_cit
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    current_year = _dt.now(tz=_UTC).year
    total = len(papers)
    if total == 0:
        return papers

    def _score(paper: Paper, idx: int) -> float:
        pos = 1.0 - idx / total
        year = paper.year or _BASE_YEAR
        recency = max(0.0, (year - _BASE_YEAR) / max(1, current_year - _BASE_YEAR))
        citation_count = paper.citation_count or 0
        citation = min(1.0, citation_count / 100.0)
        return pos * _POSITION_WEIGHT + recency * _RECENCY_WEIGHT + citation * _CITATION_WEIGHT

    indexed = list(enumerate(papers))
    indexed.sort(key=lambda t: _score(t[1], t[0]), reverse=True)
    return [p for _, p in indexed]
