"""Citation Manager v1 service — BRD FR-1.5.

Resolves the citation keys used in a drafted section against the approved-paper
pool, so the review panel can show: which keys were cited, which are
*unresolved* (cited but NOT in the pool — i.e. a likely hallucination), and the
human-readable metadata for the resolved ones.

Reuses the Scribe's `[@key]` extractor so "what counts as a citation" stays in
one place (app/agents/scribe.py). Pure-ish: takes a session + project, no graph
state — unit-testable against a plain DB.
"""

from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.scribe import _extract_cited_keys
from app.models.db import ArtifactRow, PaperRow


class ResolvedCitation(TypedDict):
    citation_key: str
    title: str
    authors: list[str]
    year: int | None
    source: str
    url: str | None


class CitationPanel(TypedDict):
    cited_keys: list[str]
    unresolved_keys: list[str]
    resolved: list[ResolvedCitation]


class SectionCitationPanel(CitationPanel):
    section: str


def _source_url(paper: PaperRow) -> str | None:
    """Best resolvable URL for a paper (pdf_url > DOI > source-specific)."""
    if paper.pdf_url:
        return str(paper.pdf_url)
    ext = (paper.external_id or "").strip()
    if not ext:
        return None
    if ext.startswith("10."):  # DOI shape
        return f"https://doi.org/{ext}"
    if paper.source == "arxiv":
        return f"https://arxiv.org/abs/{ext}"
    if paper.source == "semantic_scholar":
        return f"https://www.semanticscholar.org/paper/{ext}"
    return None


async def _latest_section_artifact(
    db: AsyncSession, project_id: UUID, section: str
) -> ArtifactRow | None:
    """The most recent section-kind artifact whose label is `section`."""
    return (
        (
            await db.execute(
                select(ArtifactRow)
                .where(
                    ArtifactRow.project_id == project_id,
                    ArtifactRow.kind == "section",
                    ArtifactRow.label == section,
                )
                .order_by(ArtifactRow.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def _approved_pool(db: AsyncSession, project_id: UUID) -> list[PaperRow]:
    return list(
        (
            await db.execute(
                select(PaperRow).where(
                    PaperRow.project_id == project_id, PaperRow.approved.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )


def apply_citation_corrections(content: str, corrections: dict[str, str]) -> str:
    """Replace `[@bad]` markers with `[@good]` per the corrections map (FR-1.5).

    Only rewrites the exact `[@<bad>]` token so we never touch prose that merely
    contains the key as plain text. Order-independent (keys are distinct markers).
    """
    out = content
    for bad, good in corrections.items():
        out = out.replace(f"[@{bad}]", f"[@{good}]")
    return out


def citations_for_content(content: str, pool: list[PaperRow]) -> CitationPanel:
    """Core resolver — pure function over content + pool. Returns the panel
    payload: cited_keys, unresolved_keys, resolved[] (with metadata)."""
    cited = _extract_cited_keys(content)
    by_key = {p.citation_key: p for p in pool}
    unresolved = [k for k in cited if k not in by_key]
    resolved: list[ResolvedCitation] = [
        {
            "citation_key": p.citation_key,
            "title": p.title,
            "authors": list(p.authors or []),
            "year": p.year,
            "source": p.source,
            "url": _source_url(p),
        }
        for k in cited
        if (p := by_key.get(k)) is not None
    ]
    return {
        "cited_keys": cited,
        "unresolved_keys": unresolved,
        "resolved": resolved,
    }


async def resolve_section_citations(
    db: AsyncSession, project_id: UUID, section: str
) -> SectionCitationPanel:
    """Full panel payload for a section's latest draft (FR-1.5)."""
    artifact = await _latest_section_artifact(db, project_id, section)
    content = artifact.content if artifact is not None else ""
    pool = await _approved_pool(db, project_id)
    out = citations_for_content(content, pool)
    return {"section": section, **out}


async def unresolved_citation_keys(db: AsyncSession, project_id: UUID, section: str) -> list[str]:
    """Just the unresolved (offending) keys for the section's latest draft —
    used by the approve gate to decide whether to block."""
    payload = await resolve_section_citations(db, project_id, section)
    return payload["unresolved_keys"]


async def _latest_section_any(db: AsyncSession, project_id: UUID) -> ArtifactRow | None:
    """The single most-recent section artifact for the project, regardless of
    which section — i.e. the one just drafted and awaiting approval."""
    return (
        (
            await db.execute(
                select(ArtifactRow)
                .where(ArtifactRow.project_id == project_id, ArtifactRow.kind == "section")
                .order_by(ArtifactRow.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def latest_section_unresolved(db: AsyncSession, project_id: UUID) -> list[str]:
    """Unresolved citation keys in the project's most-recent section draft —
    the approve gate uses this without needing the section name from run state."""
    artifact = await _latest_section_any(db, project_id)
    if artifact is None:
        return []
    pool = await _approved_pool(db, project_id)
    out = citations_for_content(artifact.content, pool)
    return out["unresolved_keys"]
