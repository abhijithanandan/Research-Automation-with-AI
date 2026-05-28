"""Tests for the Phase-4 refinement round (mandate 2026-05-26):

1. The Scribe prompt receives workflow_telemetry verbatim and is steered
   away from "human cosplay" methodology fabrication (BRD §10 risk
   mitigation — academic integrity).
2. node_assemble appends a real ``## References`` section resolving every
   ``[@key]`` marker in the body back to the approved pool, plus an
   AI-disclosure preamble at the top of the manuscript.

These tests do NOT call any LLM. They exercise the prompt-construction
path and the deterministic post-processing in node_assemble.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.agents.scribe import Scribe, ScribeInput
from app.graph.workflow import (
    _build_disclosure_block,
    _build_references_section,
    node_assemble,
)
from app.models.schemas import Paper, Phase

TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000060")


def _paper(citation_key: str, title: str, **overrides: Any) -> Paper:
    base = {
        "id": uuid4(),
        "project_id": TEST_PROJECT_ID,
        "source": "arxiv",
        "external_id": f"arxiv:{citation_key}",
        "title": title,
        "authors": ["Author, A.", "Author, B."],
        "year": 2024,
        "abstract": f"Abstract for {title}.",
        "citation_key": citation_key,
        "approved": True,
        "added_at": datetime.now(tz=UTC),
    }
    base.update(overrides)
    return Paper(**base)  # type: ignore[arg-type]


def _paper_dict(citation_key: str, **overrides: Any) -> dict[str, Any]:
    """Approved-pool entry shape — matches how the graph stores them in state."""
    base = {
        "id": str(uuid4()),
        "project_id": str(TEST_PROJECT_ID),
        "source": "arxiv",
        "external_id": f"{citation_key}-arxivid",
        "title": f"Title for {citation_key}",
        "authors": ["Smith, J.", "Jones, K."],
        "year": 2024,
        "abstract": "abs",
        "pdf_url": None,
        "citation_key": citation_key,
        "citation_count": None,
        "approved": True,
        "added_at": datetime.now(tz=UTC).isoformat(),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Scribe prompt — anti-cosplay + telemetry threading
# ---------------------------------------------------------------------------


def _build_methodology_prompt(telemetry: dict[str, Any]) -> str:
    """Helper — invoke Scribe._build_prompt for the methodology section."""
    return Scribe._build_prompt(
        ScribeInput(
            section="methodology",
            approved_pool=[_paper("alpha2024", "Alpha"), _paper("beta2024", "Beta")],
            prior_sections=[],
            workflow_telemetry=telemetry,
        ),
        approved_keys=["alpha2024", "beta2024"],
        rag_context="",
        feedback=None,
    )


def test_methodology_prompt_includes_real_telemetry_verbatim() -> None:
    """The Methodology prompt must surface the *actual* sources queried, the
    candidate count, and the approved count — so the Scribe can write a
    truthful methodology rather than a fabricated one."""
    telemetry = {
        "sources_queried": ["semantic_scholar", "arxiv", "crossref"],
        "sources_with_hits": ["semantic_scholar", "arxiv"],
        "expanded_queries": ["q1", "q2"],
        "candidate_count": 30,
        "approved_count": 4,
    }
    prompt = _build_methodology_prompt(telemetry)
    # The exact telemetry values must appear in the prompt so the model
    # can copy them into the prose verbatim.
    assert "semantic_scholar" in prompt
    assert "arxiv" in prompt
    assert "crossref" in prompt
    assert "candidate_count: 30" in prompt
    assert "approved_count: 4" in prompt


def test_methodology_prompt_explicitly_forbids_human_cosplay() -> None:
    """The system block must instruct the LLM not to fabricate manual
    human-style literature-review steps (IEEE Xplore searches, manual
    screening etc.). This is the academic-integrity guardrail from BRD §10."""
    prompt = _build_methodology_prompt({"sources_queried": ["arxiv"]})
    # Search for the hard-rule keywords. Spelling matters because this is
    # the verbatim instruction passed to the LLM.
    lowered = prompt.lower()
    assert "do not invent manual search steps" in lowered
    assert "ieee xplore" in lowered  # the negative example must be present


def test_all_section_prompts_carry_pool_size_substitution() -> None:
    """Every section prompt must mention `{pool_size}` (e.g. "the 4 surveyed
    papers") so the Scribe hedges in proportion to the actual pool."""
    pool = [_paper(f"p{i}2024", f"Paper {i}") for i in range(3)]
    for section in (
        "abstract",
        "introduction",
        "related_work",
        "methodology",
        "results",
        "discussion",
        "conclusion",
    ):
        prompt = Scribe._build_prompt(
            ScribeInput(
                section=section,  # type: ignore[arg-type]
                approved_pool=pool,
                prior_sections=[],
                workflow_telemetry={"approved_count": 3},
            ),
            approved_keys=[p.citation_key for p in pool],
            rag_context="",
            feedback=None,
        )
        # pool_size literal "3" must appear at least once (either in the
        # anti-cosplay block via approved_count or in the section prefix).
        assert "3" in prompt, f"section {section} prompt lacks pool_size disclosure"


def test_prompt_carries_hedging_instructions() -> None:
    """The system block must tell the LLM to hedge with small pools and not
    to make sweeping field-wide claims (BRD §10 risk mitigation)."""
    prompt = _build_methodology_prompt({})
    lowered = prompt.lower()
    assert "academic hedging" in lowered
    assert "prefer phrases like" in lowered
    # Negative-example check: the prompt must list the forbidden phrasings.
    assert "headline finding is that" in lowered
    assert "proves" in lowered


# ---------------------------------------------------------------------------
# 2. References generator — _build_references_section
# ---------------------------------------------------------------------------


def test_references_resolves_cited_keys_in_first_occurrence_order() -> None:
    pool = [
        _paper_dict("beta2024", title="Beta paper"),
        _paper_dict("alpha2024", title="Alpha paper"),
        _paper_dict("gamma2024", title="Gamma paper"),
    ]
    body = (
        "Alpha et al. [@alpha2024] introduced an approach. Beta et al. [@beta2024] "
        "refined it; later Gamma [@gamma2024] disputed both [@alpha2024]."
    )
    refs = _build_references_section(pool, body)
    # Order of first occurrence: alpha, beta, gamma.
    assert refs.index("[@alpha2024]") < refs.index("[@beta2024]")
    assert refs.index("[@beta2024]") < refs.index("[@gamma2024]")
    # Numbering follows reading order.
    assert "1. **[@alpha2024]**" in refs
    assert "2. **[@beta2024]**" in refs
    assert "3. **[@gamma2024]**" in refs


def test_references_includes_authors_year_title_and_url() -> None:
    pool = [
        _paper_dict(
            "alpha2024",
            title="An Alpha Paper",
            authors=["Smith, J.", "Doe, A."],
            year=2024,
            pdf_url="https://arxiv.org/pdf/1234.5678",
        )
    ]
    body = "See [@alpha2024]."
    refs = _build_references_section(pool, body)
    assert "Smith, J., Doe, A." in refs
    assert "(2024)" in refs
    assert "*An Alpha Paper*" in refs
    assert "https://arxiv.org/pdf/1234.5678" in refs


def test_references_falls_back_to_doi_then_arxiv_url_then_ss_url() -> None:
    pool = [
        _paper_dict(
            "doi2024",
            external_id="10.1000/test",
            pdf_url=None,
        ),
        _paper_dict(
            "arxiv2024",
            source="arxiv",
            external_id="2401.00001",
            pdf_url=None,
        ),
        _paper_dict(
            "ss2024",
            source="semantic_scholar",
            external_id="ssid123",
            pdf_url=None,
        ),
    ]
    body = "[@doi2024] [@arxiv2024] [@ss2024]"
    refs = _build_references_section(pool, body)
    assert "https://doi.org/10.1000/test" in refs
    assert "https://arxiv.org/abs/2401.00001" in refs
    assert "https://www.semanticscholar.org/paper/ssid123" in refs


def test_references_flags_unresolved_citation_keys_separately() -> None:
    """A key in the body that isn't in the pool must surface under a clearly
    labelled subsection, not silently disappear."""
    pool = [_paper_dict("real2024")]
    body = "Real [@real2024] and ghost [@phantom2025]."
    refs = _build_references_section(pool, body)
    assert "Citations not in approved pool" in refs
    assert "phantom2025" in refs
    assert "[@real2024]" in refs


def test_references_empty_when_no_citations_in_body() -> None:
    pool = [_paper_dict("alpha2024")]
    body = "No citations in this prose."
    refs = _build_references_section(pool, body)
    assert refs == ""


# ---------------------------------------------------------------------------
# 3. AI-disclosure preamble — _build_disclosure_block
# ---------------------------------------------------------------------------


def test_disclosure_block_names_pipeline_and_pool_size() -> None:
    block = _build_disclosure_block(
        {"sources_queried": ["arxiv", "crossref"], "candidate_count": 30},
        approved_count=4,
    )
    assert "ResearchFlow AI" in block
    assert "arxiv" in block
    assert "crossref" in block
    assert "30 candidate" in block
    assert "**4 papers**" in block
    # The disclosure must explicitly cap reader expectations.
    assert "small" in block.lower()


def test_disclosure_block_handles_singular_pool_size() -> None:
    block = _build_disclosure_block({}, approved_count=1)
    assert "**1 paper**" in block  # singular
    assert "**1 papers**" not in block


def test_disclosure_block_handles_empty_telemetry() -> None:
    """A run with no captured telemetry must still produce a sensible
    disclosure (defence-in-depth: legacy state, partial failures)."""
    block = _build_disclosure_block({}, approved_count=3)
    assert "ResearchFlow AI" in block
    assert "**3 papers**" in block


# ---------------------------------------------------------------------------
# 4. node_assemble end-to-end — disclosure + references + canonical order
# ---------------------------------------------------------------------------


def _draft_with_citations(section: str, body: str) -> dict[str, Any]:
    return {
        "section": section,
        "artifact": {
            "id": str(uuid4()),
            "project_id": str(TEST_PROJECT_ID),
            "kind": "section",
            "label": section,
            "content": body,
            "mime_type": "text/markdown",
            "produced_by": "scribe",
            "parent_id": None,
            "created_at": datetime.now(tz=UTC).isoformat(),
        },
        "cited_keys": [],
    }


@pytest.mark.asyncio
async def test_assemble_appends_references_resolved_from_approved_pool() -> None:
    """End-to-end: a manuscript with citations across multiple sections must
    end with a single ``## References`` section listing every used citation
    exactly once, resolved against the approved pool."""
    pool = [
        _paper_dict("alpha2024", title="Alpha Paper", year=2024),
        _paper_dict("beta2024", title="Beta Paper", year=2023),
    ]
    drafts = [
        _draft_with_citations("abstract", "## Abstract\n\nOverview."),
        _draft_with_citations("introduction", "## Introduction\n\nAlpha said [@alpha2024]."),
        _draft_with_citations(
            "related_work",
            "## Related Work\n\nBoth [@alpha2024] and [@beta2024] are relevant.",
        ),
        _draft_with_citations("methodology", "## Methodology\n\nAgentic pipeline."),
        _draft_with_citations("results", "## Results\n\nFindings."),
        _draft_with_citations("discussion", "## Discussion\n\nThoughts."),
        _draft_with_citations("conclusion", "## Conclusion\n\nWrap."),
    ]
    state = {
        "project_id": TEST_PROJECT_ID,
        "workflow_run_id": uuid4(),
        "phase": Phase.DRAFTING,
        "drafts": drafts,
        "sections_done": [d["section"] for d in drafts],
        "sections_remaining": [],
        "approved_pool": pool,
        "workflow_telemetry": {
            "sources_queried": ["semantic_scholar", "arxiv"],
            "candidate_count": 30,
            "approved_count": 2,
        },
    }
    out = await node_assemble(state)  # type: ignore[arg-type]
    manuscript = out["manuscript"]
    assert manuscript is not None
    content = manuscript["content"]

    # References section appears once, at the end (after all sections).
    refs_idx = content.find("## References")
    conclusion_idx = content.find("## Conclusion")
    assert refs_idx > conclusion_idx, "References must appear after the body"
    assert content.count("## References") == 1

    # Both cited keys resolved with their pool titles.
    assert "*Alpha Paper*" in content
    assert "*Beta Paper*" in content
    # Numbering reflects first-occurrence order across sections.
    assert "1. **[@alpha2024]**" in content
    assert "2. **[@beta2024]**" in content


@pytest.mark.asyncio
async def test_assemble_prepends_ai_disclosure_block() -> None:
    """The disclosure preamble must sit between the title page and the body
    so a reader cannot miss the AI provenance + pool-size caveat."""
    pool = [_paper_dict("alpha2024")]
    drafts = [
        _draft_with_citations("abstract", "## Abstract\n\nOverview [@alpha2024]."),
        _draft_with_citations("introduction", "## Introduction\n\nIntro."),
        _draft_with_citations("related_work", "## Related Work\n\nWork."),
        _draft_with_citations("methodology", "## Methodology\n\nMethods."),
        _draft_with_citations("results", "## Results\n\nResults."),
        _draft_with_citations("discussion", "## Discussion\n\nDiscussion."),
        _draft_with_citations("conclusion", "## Conclusion\n\nConclusion."),
    ]
    state = {
        "project_id": TEST_PROJECT_ID,
        "workflow_run_id": uuid4(),
        "phase": Phase.DRAFTING,
        "drafts": drafts,
        "sections_done": [d["section"] for d in drafts],
        "sections_remaining": [],
        "approved_pool": pool,
        "workflow_telemetry": {"sources_queried": ["arxiv"], "approved_count": 1},
    }
    out = await node_assemble(state)  # type: ignore[arg-type]
    content = out["manuscript"]["content"]

    title_idx = content.find("# Manuscript")
    disclosure_idx = content.find("AI-generated review — disclosure")
    body_idx = content.find("## Abstract")
    references_idx = content.find("## References")

    # Order: title page → disclosure → body → references.
    assert title_idx >= 0
    assert title_idx < disclosure_idx < body_idx < references_idx
    # Disclosure prominently states pool size and pipeline name.
    assert "**1 paper**" in content
    assert "ResearchFlow AI" in content


@pytest.mark.asyncio
async def test_assemble_no_references_block_when_body_has_no_citations() -> None:
    """A run where the Scribe didn't cite anything (degraded mode, mock LLM)
    must NOT produce an empty ``## References`` section."""
    pool = [_paper_dict("alpha2024")]
    drafts = [
        _draft_with_citations(s, f"## {s.title()}\n\nNo cites here.")
        for s in (
            "abstract",
            "introduction",
            "related_work",
            "methodology",
            "results",
            "discussion",
            "conclusion",
        )
    ]
    state = {
        "project_id": TEST_PROJECT_ID,
        "workflow_run_id": uuid4(),
        "phase": Phase.DRAFTING,
        "drafts": drafts,
        "sections_done": [d["section"] for d in drafts],
        "sections_remaining": [],
        "approved_pool": pool,
        "workflow_telemetry": {"approved_count": 1},
    }
    out = await node_assemble(state)  # type: ignore[arg-type]
    content = out["manuscript"]["content"]
    assert "## References" not in content


# ---------------------------------------------------------------------------
# 5. ScribeInput is strictly typed — Mypy guard (no # type: ignore in code path)
# ---------------------------------------------------------------------------


def test_scribe_input_accepts_typed_workflow_telemetry() -> None:
    """Sanity check that ScribeInput's workflow_telemetry field is a dict
    (not Any-untyped) — proves the mypy contract."""
    payload = ScribeInput(
        section="abstract",
        approved_pool=[_paper("alpha2024", "Alpha")],
        prior_sections=[],
        workflow_telemetry={"approved_count": 1},
    )
    assert payload.workflow_telemetry == {"approved_count": 1}
    # Default factory keeps it as a dict (not None) when omitted.
    bare = ScribeInput(
        section="abstract",
        approved_pool=[_paper("alpha2024", "Alpha")],
        prior_sections=[],
    )
    assert bare.workflow_telemetry == {}
