"""The Librarian agent — discovery. See SPEC.md §6.1 and docs/agents/librarian.md.

Implements all 6 behaviors from the agent contract:
  1. Query expansion via LLMGateway (with ArXiv category taxonomy alignment).
  2. Source fan-out via DiscoveryService (never calls httpx directly).
  3. Deduplication by DOI then fuzzy title (token-set ratio ≥ 0.9).
  4. Ranking by relevance + citation velocity + recency.
  5. Trimming to max_candidates.
  6. Citation key generation (firstauthorlastnameYEAR, disambiguated with a/b/…).

Every call writes to audit_log before returning (caller must pass audit_writer).
"""

from __future__ import annotations

import json
import math
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
You are a research librarian. Given the seed query below, produce:
1. {n} alternative keyword search queries.
2. A list of relevant ArXiv category codes (e.g. cs.CV, cs.LG, stat.ML).

Seed query: {seed_query}
"""


class ExpandedSearch(BaseModel):
    """Structured output schema for LLM query expansion."""

    queries: list[str]
    arxiv_categories: list[str]


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
    arxiv_categories: list[str]


class Librarian(Agent[LibrarianInput, LibrarianOutput]):
    name = "librarian"

    def __init__(self) -> None:
        self._discovery = DiscoveryService()
        self._llm = get_llm_gateway()

    async def run(self, payload: LibrarianInput) -> LibrarianOutput:
        # 1. Query expansion -------------------------------------------------
        expansion = await self._expand_query(payload.seed_query)
        expanded_queries = expansion.queries
        arxiv_categories = expansion.arxiv_categories
        all_queries = [payload.seed_query, *expanded_queries]
        _log.info(
            "librarian_queries",
            seed=payload.seed_query,
            expanded=expanded_queries,
            arxiv_categories=arxiv_categories,
        )

        # 2. Source fan-out ---------------------------------------------------
        raw_candidates = await self._discovery.search(
            queries=all_queries,
            max_per_source=max(payload.max_candidates, 50),
            arxiv_categories=arxiv_categories,
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
        return LibrarianOutput(
            candidates=trimmed,
            expanded_queries=expanded_queries,
            arxiv_categories=arxiv_categories,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _expand_query(self, seed_query: str) -> ExpandedSearch:
        """Use the LLM to generate alternative queries and ArXiv categories.

        Falls back to empty lists if the LLM provider is unavailable --
        the Librarian still runs with the seed query alone (see failure modes).
        """
        prompt = _EXPANSION_PROMPT_TEMPLATE.format(n=4, seed_query=seed_query)
        try:
            from google.genai import types as genai_types

            config = genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExpandedSearch,
            )
            text, _telemetry = await self._llm.complete(prompt, config=config)
            data = json.loads(text)
            queries = [str(q) for q in data.get("queries", [])[:5]]
            categories = [str(c) for c in data.get("arxiv_categories", [])[:5]]
            return ExpandedSearch(queries=queries, arxiv_categories=categories)
        except Exception as exc:
            _log.warning("librarian_expansion_failed", error=str(exc))
            return ExpandedSearch(queries=[], arxiv_categories=[])


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------

_FUZZY_RATIO_THRESHOLD = 90  # token_set_ratio >= 90 -> treat as duplicate


def _normalise_doi(doi: str | None) -> str:
    if not doi:
        return ""
    # Use removeprefix to avoid the B005 lstrip multi-char issue.
    normalised = doi.lower().strip()
    normalised = normalised.removeprefix("https://doi.org/")
    normalised = normalised.removeprefix("http://dx.doi.org/")
    return normalised


def _deduplicate(papers: list[Paper]) -> list[Paper]:
    """Remove duplicates by DOI first, then by fuzzy title.

    Keeps the first occurrence (prefer Semantic Scholar ordering which tends
    to be more complete). This mirrors the contract in docs/agents/librarian.md.

    DOI detection uses source=="semantic_scholar" as a proxy — Semantic Scholar
    always sets external_id to the DOI when one exists (extracted from externalIds["DOI"]).
    ArXiv papers never have a DOI in external_id (it's an arxiv ID like "2310.12345"),
    so checking "10." in external_id was fragile (2310.12345 matches!).
    """
    seen_dois: set[str] = set()
    unique: list[Paper] = []

    for paper in papers:
        # Only treat external_id as a DOI when the source explicitly provides one.
        # Semantic Scholar sets external_id from externalIds["DOI"] when present.
        # ArXiv IDs (e.g. "2310.12345") look like DOIs but are not.
        is_doi = paper.source == "semantic_scholar" and paper.external_id.startswith("10.")
        doi = _normalise_doi(paper.external_id if is_doi else None)

        if doi:
            if doi in seen_dois:
                continue
            seen_dois.add(doi)
            unique.append(paper)
            continue

        # No DOI -- check fuzzy title against existing unique titles.
        title_normalised = paper.title.lower().strip()
        is_dup = False
        for existing in unique:
            ratio: int = fuzz.token_set_ratio(title_normalised, existing.title.lower().strip())
            if ratio >= _FUZZY_RATIO_THRESHOLD:
                is_dup = True
                break

        if not is_dup:
            unique.append(paper)

    return unique


# ---------------------------------------------------------------------------
# Ranking helpers — citation velocity heuristic
# ---------------------------------------------------------------------------

_VELOCITY_WEIGHT = 0.5
_RECENCY_WEIGHT = 0.3
_POSITION_WEIGHT = 0.2
_BASE_YEAR = 2000


def _citation_velocity(citation_count: int, paper_year: int, current_year: int) -> float:
    """Compute time-normalised citation velocity.

    velocity = citations / paper_age_years  (capped at 1.0 after log scaling)

    This balances seminal papers with high raw counts against recent
    breakthroughs with fewer but faster-accumulating citations.
    """
    age = max(1, current_year - paper_year + 1)  # +1 avoids div-by-zero for current year
    raw_velocity = citation_count / age
    # Log-scale and cap to [0, 1] — log1p(50)/log1p(50)=1.0 is the saturation point.
    return min(1.0, math.log1p(raw_velocity) / math.log1p(50))


def _rank(papers: list[Paper]) -> list[Paper]:
    """Score each paper and return sorted descending.

    Score = position_score * W_pos + recency_score * W_rec + velocity_score * W_vel

    Citation velocity replaces the raw citation count bump, normalising
    by paper age so that a 2024 paper with 50 citations ranks higher than
    a 2005 paper with 200 citations.
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
        velocity = _citation_velocity(paper.citation_count or 0, year, current_year)
        return pos * _POSITION_WEIGHT + recency * _RECENCY_WEIGHT + velocity * _VELOCITY_WEIGHT

    indexed = list(enumerate(papers))
    indexed.sort(key=lambda t: _score(t[1], t[0]), reverse=True)
    return [p for _, p in indexed]
