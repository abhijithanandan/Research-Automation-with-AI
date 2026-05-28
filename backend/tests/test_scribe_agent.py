"""Tests for the Scribe agent (Phase 4 B-3).

docs/agents/scribe.md §Tests required:
  - test_scribe_produces_section_with_valid_citations
  - test_scribe_retries_once_on_invalid_citations
  - test_scribe_surfaces_invalid_citations_after_second_failure
  - test_scribe_rejects_latex_format_in_v01

The Scribe writes one section at a time from the approved paper pool.
Every cited key must belong to the pool; the agent re-prompts once with
feedback on validation failure and surfaces remaining offenders with an
"INVALID:" prefix on the second failure (per plan B-3).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.agents.scribe import Scribe, ScribeInput
from app.models.schemas import Paper

TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000020")


def _paper(citation_key: str, title: str) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=TEST_PROJECT_ID,
        source="arxiv",  # type: ignore[arg-type]
        external_id=f"arxiv:{citation_key}",
        title=title,
        authors=["Author, A"],
        year=2024,
        abstract=f"Abstract for {title}.",
        citation_key=citation_key,
        approved=True,
        added_at=datetime.now(tz=UTC),
    )


class _FakeLLM:
    """Stand-in for LLMGateway.complete().

    Each call returns the next pre-canned response; `calls` records every
    prompt so tests can assert on feedback injection on the retry path.
    """

    def __init__(self, responses: list[str]) -> None:
        self.calls: list[str] = []
        self._responses = list(responses)

    async def complete(self, prompt: str, **kwargs: object) -> tuple[str, dict[str, object]]:
        self.calls.append(prompt)
        text = self._responses.pop(0) if self._responses else ""
        telemetry: dict[str, object] = {"tokens_in": 1, "tokens_out": 1, "cost_usd": None}
        return text, telemetry


class _FakeVectorStore:
    """No-op vector store — Scribe's RAG path is non-fatal when no chunks return."""

    async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None:
        return None

    async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]:
        return []


# ---------------------------------------------------------------------------
# B-3 required tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scribe_produces_section_with_valid_citations() -> None:
    """When the LLM cites only keys from the approved pool, the section is
    returned as-is and `cited_keys` matches the citations in the markdown."""
    papers = [_paper("alpha2024", "Alpha"), _paper("beta2024", "Beta")]
    content = (
        "## Related Work\n\n"
        "Alpha et al. [@alpha2024] proposed a method; Beta et al. [@beta2024] refined it."
    )
    scribe = Scribe(llm=_FakeLLM([content]), vector_store=_FakeVectorStore())

    out = await scribe.run(
        ScribeInput(section="related_work", approved_pool=papers, prior_sections=[])
    )

    assert out.section.kind == "section"
    assert out.section.label == "related_work"
    assert out.section.mime_type == "text/markdown"
    assert out.section.produced_by == "scribe"
    assert set(out.cited_keys) == {"alpha2024", "beta2024"}
    # No INVALID: prefix — clean validation pass.
    assert not any(k.startswith("INVALID:") for k in out.cited_keys)


@pytest.mark.asyncio
async def test_scribe_retries_once_on_invalid_citations() -> None:
    """First LLM call cites a key not in the pool; Scribe re-prompts with
    feedback and the second call cites a valid key. Two LLM calls total; the
    second prompt must contain the feedback string."""
    papers = [_paper("alpha2024", "Alpha")]
    bad = "## Related Work\n\nFoo [@nonexistent2024] et al."
    good = "## Related Work\n\nAlpha [@alpha2024] et al."
    fake = _FakeLLM([bad, good])
    scribe = Scribe(llm=fake, vector_store=_FakeVectorStore())

    out = await scribe.run(
        ScribeInput(section="related_work", approved_pool=papers, prior_sections=[])
    )

    assert len(fake.calls) == 2, "Scribe must retry exactly once on invalid citations"
    # The retry prompt must mention the offending key so the LLM knows what
    # to fix — per docs/agents/scribe.md.
    assert "nonexistent2024" in fake.calls[1]
    # Final output is the clean second draft.
    assert set(out.cited_keys) == {"alpha2024"}
    assert not any(k.startswith("INVALID:") for k in out.cited_keys)


@pytest.mark.asyncio
async def test_scribe_surfaces_invalid_citations_after_second_failure() -> None:
    """Both LLM calls cite unknown keys; Scribe returns the second draft anyway
    with the offenders flagged so the frontend can render a warning."""
    papers = [_paper("alpha2024", "Alpha")]
    bad1 = "## Related Work\n\nFoo [@nonexistent2024]."
    bad2 = "## Related Work\n\nBar [@stillbad2024]."
    fake = _FakeLLM([bad1, bad2])
    scribe = Scribe(llm=fake, vector_store=_FakeVectorStore())

    out = await scribe.run(
        ScribeInput(section="related_work", approved_pool=papers, prior_sections=[])
    )

    assert len(fake.calls) == 2
    # Second draft is returned as-is — the user can override or reject.
    assert "stillbad2024" in out.section.content
    # The offending key is surfaced in cited_keys with an INVALID: prefix
    # so the frontend can show a warning chip beside the approve button.
    invalid = [k for k in out.cited_keys if k.startswith("INVALID:")]
    assert invalid, "Second-failure offenders must be flagged with INVALID: prefix"
    assert any("stillbad2024" in k for k in invalid)


@pytest.mark.asyncio
async def test_scribe_rejects_latex_format_in_v01() -> None:
    """Phase 4 ships markdown only (BRD §8). LaTeX is v0.2; the agent must
    raise a typed error rather than silently producing broken output."""
    papers = [_paper("alpha2024", "Alpha")]
    scribe = Scribe(llm=_FakeLLM(["whatever"]), vector_store=_FakeVectorStore())

    with pytest.raises(NotImplementedError, match=r"(?i)latex"):
        await scribe.run(
            ScribeInput(
                section="abstract",
                approved_pool=papers,
                prior_sections=[],
                output_format="latex",
            )
        )


# ---------------------------------------------------------------------------
# Citation validator unit (extends test_scribe_citation_validator.py with
# a positive-case sanity check so the validator stays a first-class helper).
# ---------------------------------------------------------------------------


def test_validate_citations_returns_offenders_only() -> None:
    papers = [_paper("alpha2024", "Alpha"), _paper("beta2024", "Beta")]
    offenders = Scribe.validate_citations(
        ["alpha2024", "ghost2024", "beta2024", "phantom2025"], papers
    )
    assert offenders == {"ghost2024", "phantom2025"}
