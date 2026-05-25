"""Tests for the Critic agent (Phase 2 B-2).

docs/agents/critic.md §Tests required:
  - test_critic_matrix_contains_every_paper
  - test_critic_regenerates_with_feedback
  - test_critic_handles_extraction_failure_gracefully

The Critic reads the approved paper pool, extracts five attributes per paper
(problem, method, dataset, key_findings, limitations), and produces a matrix
artifact (JSON) plus a narrative summary artifact (markdown).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.agents.critic import Critic, CriticInput
from app.models.schemas import Paper

TEST_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000010")


def _paper(citation_key: str, title: str) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=TEST_PROJECT_ID,
        source="arxiv",
        external_id=f"arxiv:{citation_key}",
        title=title,
        authors=["Author, A"],
        year=2024,
        abstract=f"Abstract for {title}.",
        citation_key=citation_key,
        approved=True,
        added_at=datetime.now(tz=UTC),
    )


def _extraction_json(citation_key: str) -> str:
    """A well-formed per-paper extraction JSON payload from the mocked LLM."""
    return json.dumps(
        {
            "citation_key": citation_key,
            "problem": "The problem.",
            "method": "The method.",
            "dataset": "The dataset.",
            "key_findings": "The findings.",
            "limitations": "The limitations.",
        }
    )


class _FakeLLM:
    """Stand-in for LLMGateway.

    `complete` returns a per-paper extraction JSON for extraction prompts and a
    narrative string for the synthesis prompt. `calls` records every prompt so
    tests can assert on feedback injection.
    """

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail_for = fail_for or set()

    async def complete(self, prompt: str, **kwargs: object) -> tuple[str, dict[str, object]]:
        self.calls.append(prompt)
        telemetry: dict[str, object] = {"tokens_in": 1, "tokens_out": 1, "cost_usd": None}
        # Synthesis prompt — identified by the marker the Critic puts in it.
        if "narrative" in prompt.lower() or "synthesis" in prompt.lower():
            return "## Synthesis\n\nA narrative grouped by method.", telemetry
        # Extraction prompt — find which paper it is about; fail if requested.
        for key in self._fail_for:
            if key in prompt:
                raise RuntimeError(f"LLM extraction failed for {key}")
        for line in prompt.splitlines():
            for token in line.replace(":", " ").split():
                if token.endswith("2024") or token.endswith("2023"):
                    return _extraction_json(token), telemetry
        # Fallback — first citation key seen.
        return _extraction_json("unknown2024"), telemetry


class _FakeVectorStore:
    """Vector store that succeeds silently (RAG available path)."""

    async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None:
        return None

    async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]:
        return []


@pytest.mark.asyncio
async def test_critic_matrix_contains_every_paper() -> None:
    """Every approved paper must appear as a row in the matrix (no silent drops)."""
    papers = [
        _paper("alpha2024", "Alpha Paper"),
        _paper("beta2024", "Beta Paper"),
        _paper("gamma2023", "Gamma Paper"),
    ]
    critic = Critic(llm=_FakeLLM(), vector_store=_FakeVectorStore())

    out = await critic.run(CriticInput(approved_papers=papers))

    matrix_data = json.loads(out.matrix.content)
    keys = {row["citation_key"] for row in matrix_data["rows"]}
    assert keys == {"alpha2024", "beta2024", "gamma2023"}
    assert out.matrix.kind == "matrix"
    assert out.matrix.mime_type == "application/json"
    assert out.summary.kind == "summary"
    assert out.summary.mime_type == "text/markdown"


@pytest.mark.asyncio
async def test_critic_regenerates_with_feedback() -> None:
    """When feedback is supplied it must be injected into the LLM prompts."""
    papers = [_paper("alpha2024", "Alpha Paper")]
    fake_llm = _FakeLLM()
    critic = Critic(llm=fake_llm, vector_store=_FakeVectorStore())

    feedback = "Focus more on the limitations of each method."
    await critic.run(CriticInput(approved_papers=papers, feedback=feedback))

    assert any(feedback in prompt for prompt in fake_llm.calls), (
        "feedback string must be injected into at least one LLM prompt"
    )


@pytest.mark.asyncio
async def test_critic_handles_extraction_failure_gracefully() -> None:
    """A per-paper LLM failure marks that row extraction_failed; node does not crash."""
    papers = [
        _paper("good2024", "Good Paper"),
        _paper("bad2024", "Bad Paper"),
    ]
    # The LLM raises only for the 'bad2024' extraction prompt.
    critic = Critic(llm=_FakeLLM(fail_for={"bad2024"}), vector_store=_FakeVectorStore())

    out = await critic.run(CriticInput(approved_papers=papers))

    matrix_data = json.loads(out.matrix.content)
    rows = {row["citation_key"]: row for row in matrix_data["rows"]}
    # Both papers still present — invariant holds.
    assert set(rows) == {"good2024", "bad2024"}
    assert rows["good2024"]["extraction_failed"] is False
    assert rows["bad2024"]["extraction_failed"] is True
    assert rows["bad2024"]["error"]


@pytest.mark.asyncio
async def test_critic_survives_vector_store_unavailable() -> None:
    """If ChromaDB is down the Critic falls back to abstract-only extraction."""
    from app.services.vector_store import VectorStoreUnavailableError

    class _DownVectorStore:
        async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None:
            raise VectorStoreUnavailableError("chroma down")

        async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]:
            raise VectorStoreUnavailableError("chroma down")

    papers = [_paper("alpha2024", "Alpha Paper")]
    critic = Critic(llm=_FakeLLM(), vector_store=_DownVectorStore())

    # Must not raise — the workflow never hard-fails on a vector-store outage.
    out = await critic.run(CriticInput(approved_papers=papers))
    matrix_data = json.loads(out.matrix.content)
    assert len(matrix_data["rows"]) == 1


@pytest.mark.asyncio
async def test_critic_accumulates_token_usage() -> None:
    """The Critic must sum token usage across every LLM call (BRD FR-3.3).

    With 3 papers the Critic makes 3 extraction calls + 1 synthesis call; the
    _FakeLLM reports tokens_in=1, tokens_out=1 per call, so the run total is 4.
    """
    papers = [
        _paper("alpha2024", "Alpha Paper"),
        _paper("beta2024", "Beta Paper"),
        _paper("gamma2023", "Gamma Paper"),
    ]
    critic = Critic(llm=_FakeLLM(), vector_store=_FakeVectorStore())

    out = await critic.run(CriticInput(approved_papers=papers))

    # 3 extractions + 1 synthesis = 4 LLM calls.
    assert out.usage.llm_calls == 4
    assert out.usage.tokens_in == 4
    assert out.usage.tokens_out == 4


@pytest.mark.asyncio
async def test_critic_counts_calls_even_when_extraction_fails() -> None:
    """A failed extraction still counts toward llm_calls is not required, but the
    successful calls must still be summed — a failure must not lose other usage."""
    papers = [
        _paper("good2024", "Good Paper"),
        _paper("bad2024", "Bad Paper"),
    ]
    critic = Critic(llm=_FakeLLM(fail_for={"bad2024"}), vector_store=_FakeVectorStore())

    out = await critic.run(CriticInput(approved_papers=papers))

    # good2024 extraction + synthesis succeed → 2 counted calls; bad2024 raised
    # before telemetry was recorded, so it is simply absent from the total.
    assert out.usage.llm_calls == 2
    assert out.usage.tokens_in == 2


@pytest.mark.asyncio
async def test_critic_matrix_validates_against_schema() -> None:
    """The matrix artifact JSON must validate against docs/agents/critic.schema.json
    (docs/agents/critic.md invariant)."""
    from pathlib import Path

    import jsonschema

    schema_path = Path(__file__).resolve().parents[2] / "docs" / "agents" / "critic.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    papers = [
        _paper("alpha2024", "Alpha Paper"),
        _paper("bad2024", "Bad Paper"),
    ]
    # Mix a successful and a failed extraction so both row shapes are exercised.
    critic = Critic(llm=_FakeLLM(fail_for={"bad2024"}), vector_store=_FakeVectorStore())
    out = await critic.run(CriticInput(approved_papers=papers))

    matrix_data = json.loads(out.matrix.content)
    # Raises jsonschema.ValidationError if the matrix shape drifts from the schema.
    jsonschema.validate(instance=matrix_data, schema=schema)
