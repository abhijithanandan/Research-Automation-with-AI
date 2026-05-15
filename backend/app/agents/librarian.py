"""The Librarian agent — discovery. See SPEC.md §6.1 and docs/agents/librarian.md."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.agents.base import Agent
from app.models.schemas import Paper


class LibrarianInput(BaseModel):
    seed_query: str
    max_candidates: int = 30
    sources: list[Literal["semantic_scholar", "arxiv", "crossref"]] = [
        "semantic_scholar",
        "arxiv",
    ]


class LibrarianOutput(BaseModel):
    candidates: list[Paper]
    expanded_queries: list[str]


class Librarian(Agent[LibrarianInput, LibrarianOutput]):
    name = "librarian"

    async def run(self, payload: LibrarianInput) -> LibrarianOutput:
        # TODO: query Semantic Scholar / ArXiv / Crossref via app.services.discovery.
        # TODO: dedupe by DOI + fuzzy-match titles.
        # TODO: rank candidates.
        _ = payload
        return LibrarianOutput(candidates=[], expanded_queries=[])
