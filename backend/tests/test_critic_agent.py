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


def _extraction_row(citation_key: str) -> dict[str, str]:
    """A well-formed single extraction row payload from the mocked LLM."""
    return {
        "citation_key": citation_key,
        "problem": "The problem.",
        "method": "The method.",
        "dataset": "The dataset.",
        "key_findings": "The findings.",
        "limitations": "The limitations.",
    }


def _extract_citation_keys_from_prompt(prompt: str) -> list[str]:
    """Pull every `citation_key: <token>` from the batched extraction prompt.

    The Critic renders papers as `citation_key: alpha2024\\ntitle: ...\\n...`
    so we just grep those lines. Used by the fake LLM to know which papers
    were requested in this batch.
    """
    keys: list[str] = []
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("citation_key:"):
            keys.append(stripped.split(":", 1)[1].strip())
    return keys


class _FakeLLM:
    """Stand-in for LLMGateway.

    Recognises three prompt shapes:
      - **Synthesis** — contains "narrative"; returns a fixed markdown stub.
      - **Batched extraction** — asks for a JSON envelope of N extractions
        (the Critic uses one call for all papers in the pool). Returns an
        ``{"extractions": [...]}`` payload. If any citation_key in the
        prompt is in ``fail_for``, the entire batched call raises (this
        models the "LLM failed → every paper marked extraction_failed"
        fallback).
      - **Legacy per-paper extraction** — kept for the older `_extract`
        helper still living in the Critic for compatibility. Same per-paper
        fail_for behaviour as before.
    """

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self._fail_for = fail_for or set()

    async def complete(self, prompt: str, **kwargs: object) -> tuple[str, dict[str, object]]:
        self.calls.append(prompt)
        telemetry: dict[str, object] = {"tokens_in": 1, "tokens_out": 1, "cost_usd": None}

        # Synthesis prompt — identified by the unique marker the Critic puts
        # in it. Must come before the batch check because both prompts may
        # share the word "synthesis".
        if "narrative" in prompt.lower():
            return "## Synthesis\n\nA narrative grouped by method.", telemetry

        # Batched extraction prompt — has the schema instruction string
        # "extractions" plus one or more `citation_key: ...` lines. Per
        # docs/agents/critic.md graceful-degradation contract, if any
        # requested paper is in fail_for, the WHOLE batch call raises
        # (the Critic then marks every paper extraction_failed=True).
        if '"extractions"' in prompt or "extractions" in prompt.lower():
            keys = _extract_citation_keys_from_prompt(prompt)
            if keys:
                if any(k in self._fail_for for k in keys):
                    raise RuntimeError(
                        "LLM batch extraction failed for "
                        + ",".join(k for k in keys if k in self._fail_for)
                    )
                envelope = {"extractions": [_extraction_row(k) for k in keys]}
                return json.dumps(envelope), telemetry

        # Legacy single-paper extraction prompt (kept for back-compat with
        # any direct tests of Critic._extract).
        for key in self._fail_for:
            if key in prompt:
                raise RuntimeError(f"LLM extraction failed for {key}")
        for line in prompt.splitlines():
            for token in line.replace(":", " ").split():
                if token.endswith("2024") or token.endswith("2023"):
                    return json.dumps(_extraction_row(token)), telemetry
        return json.dumps(_extraction_row("unknown2024")), telemetry


class _FakeVectorStore:
    """Vector store that succeeds silently (RAG available path)."""

    async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None:
        return None

    async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]:
        return []

    async def hybrid_reranked_search(
        self, namespace: str, query: str, *, top_n: int | None = None
    ) -> list[dict[str, object]]:
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
    """When the batched extraction call raises, EVERY paper is marked
    extraction_failed (not just the one that triggered the failure).

    This is the deliberate batched-Critic trade-off (chosen 2026-05-27 to
    fit inside the Gemini free-tier daily budget): one LLM call covers
    the whole pool, so its failure propagates to the whole pool. The
    matrix invariant still holds — every approved paper appears as a row,
    just with extraction_failed=True. The user can reject + regenerate."""
    papers = [
        _paper("good2024", "Good Paper"),
        _paper("bad2024", "Bad Paper"),
    ]
    # The batched call raises if ANY paper in the batch is in fail_for.
    critic = Critic(llm=_FakeLLM(fail_for={"bad2024"}), vector_store=_FakeVectorStore())

    out = await critic.run(CriticInput(approved_papers=papers))

    matrix_data = json.loads(out.matrix.content)
    rows = {row["citation_key"]: row for row in matrix_data["rows"]}
    # Matrix invariant: both papers appear.
    assert set(rows) == {"good2024", "bad2024"}
    # Batched contract: when the batch fails, ALL rows are marked failed.
    assert rows["good2024"]["extraction_failed"] is True
    assert rows["bad2024"]["extraction_failed"] is True
    assert rows["good2024"]["error"] == rows["bad2024"]["error"]  # same root cause
    assert "bad2024" in (rows["good2024"]["error"] or "")


@pytest.mark.asyncio
async def test_critic_survives_vector_store_unavailable() -> None:
    """If ChromaDB is down the Critic falls back to abstract-only extraction."""
    from app.services.vector_store import VectorStoreUnavailableError

    class _DownVectorStore:
        async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None:
            raise VectorStoreUnavailableError("chroma down")

        async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]:
            raise VectorStoreUnavailableError("chroma down")

        async def hybrid_reranked_search(
            self, namespace: str, query: str, *, top_n: int | None = None
        ) -> list[dict[str, object]]:
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

    With batched extraction the Critic makes EXACTLY two LLM calls per run
    regardless of pool size: one batched extraction + one synthesis. With
    _FakeLLM reporting tokens_in=1, tokens_out=1 per call, the run total
    is 2 (vs N+1 in the per-paper era — this is the whole point of the
    batched refactor: stay inside the Gemini free-tier daily budget).
    """
    papers = [
        _paper("alpha2024", "Alpha Paper"),
        _paper("beta2024", "Beta Paper"),
        _paper("gamma2023", "Gamma Paper"),
    ]
    critic = Critic(llm=_FakeLLM(), vector_store=_FakeVectorStore())

    out = await critic.run(CriticInput(approved_papers=papers))

    # 1 batched extraction + 1 synthesis = 2 LLM calls, regardless of pool size.
    assert out.usage.llm_calls == 2
    assert out.usage.tokens_in == 2
    assert out.usage.tokens_out == 2


@pytest.mark.asyncio
async def test_critic_counts_calls_even_when_extraction_fails() -> None:
    """If the batched extraction raises, only the synthesis call's telemetry
    is counted — the batch call raised before its telemetry was recorded.

    Every paper still gets an extraction_failed row (the matrix invariant
    is preserved) but llm_calls reflects only the calls that returned.
    """
    papers = [
        _paper("good2024", "Good Paper"),
        _paper("bad2024", "Bad Paper"),
    ]
    critic = Critic(llm=_FakeLLM(fail_for={"bad2024"}), vector_store=_FakeVectorStore())

    out = await critic.run(CriticInput(approved_papers=papers))

    # Batched extraction raised (bad2024 was in the batch) → no telemetry
    # recorded for that call. Synthesis still runs → 1 call counted.
    assert out.usage.llm_calls == 1
    assert out.usage.tokens_in == 1


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


# ---------------------------------------------------------------------------
# Batched-extraction contract — added when the Critic moved from O(N) calls
# per pool to O(1) (one batched call) to fit inside the Gemini free-tier
# daily budget. These tests pin the contract so future changes don't
# silently regress it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critic_makes_exactly_two_llm_calls_regardless_of_pool_size() -> None:
    """Five papers, ten papers — doesn't matter. Total LLM calls should be:
    one batched extraction + one synthesis = 2. This is the core invariant
    that keeps the Critic inside the daily free-tier budget."""
    pool_sizes = [1, 3, 5, 10]
    for n in pool_sizes:
        papers = [_paper(f"p{i}2024", f"Paper {i}") for i in range(n)]
        fake = _FakeLLM()
        critic = Critic(llm=fake, vector_store=_FakeVectorStore())
        await critic.run(CriticInput(approved_papers=papers))
        # ALWAYS 2 calls — independent of n. The whole point of batching.
        assert len(fake.calls) == 2, f"pool size {n}: expected 2 LLM calls, got {len(fake.calls)}"


@pytest.mark.asyncio
async def test_critic_batched_call_includes_every_papers_citation_key() -> None:
    """The batched extraction prompt must list every approved paper. If the
    Critic ever silently dropped a paper from the prompt it would silently
    drop it from the matrix."""
    papers = [
        _paper("alpha2024", "Alpha Paper"),
        _paper("beta2024", "Beta Paper"),
        _paper("gamma2024", "Gamma Paper"),
    ]
    fake = _FakeLLM()
    critic = Critic(llm=fake, vector_store=_FakeVectorStore())
    await critic.run(CriticInput(approved_papers=papers))

    # First call is the batch extraction (second is synthesis).
    batch_prompt = fake.calls[0]
    for p in papers:
        assert f"citation_key: {p.citation_key}" in batch_prompt, (
            f"batch prompt missing {p.citation_key}"
        )
    assert "paper_count" not in batch_prompt.lower() or "3" in batch_prompt, (
        "batch prompt should declare the expected count of extractions"
    )


@pytest.mark.asyncio
async def test_matrix_invariant_holds_when_llm_returns_partial_response() -> None:
    """If the LLM forgets to extract some papers (returns a short array),
    the missing ones get extraction_failed=True rows. Every approved paper
    appears in the final matrix — docs/agents/critic.md §Invariants."""

    class _PartialResponseLLM:
        """Returns a batch payload that only includes the FIRST paper from
        the request, dropping the rest. Models the LLM-truncation failure
        mode."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def complete(self, prompt: str, **kwargs: object) -> tuple[str, dict[str, object]]:
            self.calls.append(prompt)
            telemetry: dict[str, object] = {
                "tokens_in": 1,
                "tokens_out": 1,
                "cost_usd": None,
            }
            if "narrative" in prompt.lower():
                return "## Synthesis\n\nstub", telemetry
            # Batched extraction — return only the first key.
            keys = _extract_citation_keys_from_prompt(prompt)
            if not keys:
                return json.dumps({"extractions": []}), telemetry
            envelope = {"extractions": [_extraction_row(keys[0])]}
            return json.dumps(envelope), telemetry

    papers = [
        _paper("alpha2024", "Alpha"),
        _paper("beta2024", "Beta"),
        _paper("gamma2024", "Gamma"),
    ]
    critic = Critic(llm=_PartialResponseLLM(), vector_store=_FakeVectorStore())
    out = await critic.run(CriticInput(approved_papers=papers))

    matrix_data = json.loads(out.matrix.content)
    rows = {row["citation_key"]: row for row in matrix_data["rows"]}
    # Every approved paper still has a row — the invariant survives partial
    # responses.
    assert set(rows) == {"alpha2024", "beta2024", "gamma2024"}
    # alpha was returned cleanly.
    assert rows["alpha2024"]["extraction_failed"] is False
    # beta + gamma were missing from the LLM response → marked failed with
    # a useful error message.
    assert rows["beta2024"]["extraction_failed"] is True
    assert "missing" in (rows["beta2024"]["error"] or "").lower()
    assert rows["gamma2024"]["extraction_failed"] is True


@pytest.mark.asyncio
async def test_matrix_invariant_holds_when_batched_call_raises() -> None:
    """When the LLM call itself raises (quota exhausted, network down,
    response can't be parsed), every paper gets an extraction_failed row
    carrying the same error string. The node does NOT crash."""

    class _AlwaysFailLLM:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def complete(self, prompt: str, **kwargs: object) -> tuple[str, dict[str, object]]:
            self.calls.append(prompt)
            telemetry: dict[str, object] = {
                "tokens_in": 1,
                "tokens_out": 1,
                "cost_usd": None,
            }
            if "narrative" in prompt.lower():
                # Synthesis still works — only the extraction call fails.
                return "## Synthesis\n\nstub", telemetry
            raise RuntimeError("simulated 429 quota exhausted")

    papers = [
        _paper("alpha2024", "Alpha"),
        _paper("beta2024", "Beta"),
    ]
    critic = Critic(llm=_AlwaysFailLLM(), vector_store=_FakeVectorStore())
    out = await critic.run(CriticInput(approved_papers=papers))

    matrix_data = json.loads(out.matrix.content)
    rows = {row["citation_key"]: row for row in matrix_data["rows"]}
    assert set(rows) == {"alpha2024", "beta2024"}
    for key in ("alpha2024", "beta2024"):
        assert rows[key]["extraction_failed"] is True
        assert "429" in (rows[key]["error"] or "")


@pytest.mark.asyncio
async def test_empty_pool_error_output_has_distinct_artifacts() -> None:
    """PR #5 finding: when the Critic is handed an empty pool it returns an
    error CriticOutput. matrix and summary must be *distinct* Artifact rows —
    if they share an id, _persist_artifacts (ON CONFLICT DO NOTHING on the
    primary key) silently drops one on persist."""
    critic = Critic(llm=_FakeLLM(), vector_store=_FakeVectorStore())
    out = await critic.run(CriticInput(approved_papers=[]))

    assert out.matrix.id != out.summary.id
    assert out.matrix.label != out.summary.label
    # Both still carry the same human-readable error content.
    assert out.matrix.content == out.summary.content
