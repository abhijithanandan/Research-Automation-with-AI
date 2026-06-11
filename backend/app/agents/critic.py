"""The Critic agent — synthesis. See SPEC.md §6.2 and docs/agents/critic.md.

Given the approved paper pool, the Critic:
  1. Embeds each paper's abstract into the vector store (RAG context).
  2. Extracts five attributes per paper via a structured LLM call:
     problem, method, dataset, key_findings, limitations.
  3. Assembles a comparison matrix (JSON) — every paper appears, no drops.
  4. Synthesizes a 3-6 paragraph narrative grouped by methodological cluster.

Failure handling (docs/agents/critic.md §Failure modes):
  - A per-paper LLM failure marks that row `extraction_failed`; the node
    still completes with every paper present.
  - A vector-store outage is non-fatal — the Critic falls back to
    abstract-only extraction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel

from app.agents._prompt_safety import SYSTEM_ANCHOR, safe_tag, xml_escape
from app.agents.base import Agent
from app.models.schemas import Artifact, Paper
from app.services.llm import LLMGateway, get_llm_gateway
from app.services.vector_store import (
    VectorStore,
    VectorStoreUnavailableError,
    get_vector_store,
)
from app.utils.logging import get_logger

_log = get_logger(__name__)

_EXTRACTION_PROMPT_TEMPLATE = """\
You are a research critic. Read the paper below and extract exactly five
attributes. Be concise — one or two sentences each.

{feedback_block}Paper citation_key: {citation_key}
{paper_block}
{rag_block}
Return JSON with keys: citation_key, problem, method, dataset,
key_findings, limitations.{system_anchor}"""

# Batched extraction template — one LLM call returns a JSON array containing
# all per-paper extractions. Lets the Critic stay inside the Gemini free-tier
# daily budget (20 calls/day) on typical small-pool runs. The Scribe will
# still write seven sections, the Librarian still expands once, but the
# Critic's calls drop from O(N) to O(1).
#
# The model is instructed to emit a JSON object with one field `extractions`,
# whose value is a list of per-paper rows. Using an object envelope rather
# than a bare array makes JSON-mode parsing more reliable across providers
# (Gemini's response_schema/response_mime_type works more cleanly with
# object roots than array roots).
_BATCH_EXTRACTION_PROMPT_TEMPLATE = """\
You are a research critic. Read the {paper_count} papers below and extract
exactly five attributes per paper. Be concise — one or two sentences each.

{feedback_block}Papers (each paper begins with its citation_key):
{papers_block}
{rag_block}
Return JSON in this exact shape:

  {{
    "extractions": [
      {{"citation_key": "...", "problem": "...", "method": "...",
        "dataset": "...", "key_findings": "...", "limitations": "..."}},
      ...one object per paper, in the same order...
    ]
  }}

The "extractions" array MUST contain exactly {paper_count} entries — one for
every paper above, identified by its citation_key. Do not skip papers, do
not invent new citation_keys.{system_anchor}"""

_SYNTHESIS_PROMPT_TEMPLATE = """\
You are a research critic writing a literature synthesis (a narrative
review). Using the per-paper extractions below, write a 3-6 paragraph
narrative grouped by methodological cluster. Reference papers only by
their citation_key. Do not invent papers.

{feedback_block}{focus_block}Per-paper extractions (JSON):
{extractions_json}

Write the narrative synthesis in Markdown.{system_anchor}"""


class PaperExtraction(BaseModel):
    """Per-paper extracted attributes — one row of the comparison matrix."""

    citation_key: str
    problem: str = ""
    method: str = ""
    dataset: str = ""
    key_findings: str = ""
    limitations: str = ""
    extraction_failed: bool = False
    error: str | None = None


class MatrixModel(BaseModel):
    """The full comparison matrix — JSON-serialized into the matrix Artifact."""

    rows: list[PaperExtraction]


class _BatchExtractionsEnvelope(BaseModel):
    """LLM response shape for the batched extraction call.

    Object-root envelope (not a bare array) — keeps Gemini's
    ``response_schema=...`` mode happy and gives Pydantic something concrete
    to validate against. Internal-only; the Critic unwraps this into a flat
    ``list[PaperExtraction]`` before persisting.
    """

    extractions: list[PaperExtraction]


class CriticInput(BaseModel):
    approved_papers: list[Paper]
    focus: str | None = None
    feedback: str | None = None


class CriticUsage(BaseModel):
    """Token + cost rollup for one Critic run (BRD FR-3.3 / §4.3 audit trail).

    The Critic makes N per-paper extraction calls plus one synthesis call;
    these totals sum every LLM call so the workflow layer can write a single
    audit_log row with model + token counts.
    """

    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    llm_calls: int = 0


class CriticOutput(BaseModel):
    matrix: Artifact
    summary: Artifact
    usage: CriticUsage = CriticUsage()


class Critic(Agent[CriticInput, CriticOutput]):
    name = "critic"

    def __init__(
        self,
        llm: LLMGateway | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        # Dependencies are injectable for testing; default to the singletons.
        self._llm = llm if llm is not None else get_llm_gateway()
        self._vs = vector_store if vector_store is not None else get_vector_store()

    async def run(self, payload: CriticInput) -> CriticOutput:
        papers = payload.approved_papers
        if not papers:
            # The engine should never route here with an empty pool — surface
            # an error artifact rather than crashing (docs/agents/critic.md).
            return self._error_output("Approved pool is empty.")

        project_id = self._resolve_project_id(papers)

        # Token/cost telemetry is accumulated across every LLM call so the
        # workflow layer can write one audit_log row (BRD FR-3.3 / §4.3).
        usage = CriticUsage(model=getattr(self._llm, "model_name", None))

        # 1. Embed -----------------------------------------------------------
        rag_available = await self._embed(project_id, papers)

        # 2. Extract attributes for ALL papers in one batched LLM call.
        # The per-paper alternative (one call per paper) was correct but
        # blew through the Gemini free-tier daily budget on pools of 5+
        # papers. Batching keeps the every-paper invariant — on LLM
        # failure or partial response, missing rows are filled in as
        # `extraction_failed=True` so the matrix still has one row per
        # approved paper (docs/agents/critic.md §Invariants).
        rows = await self._extract_batch(project_id, papers, payload.feedback, rag_available, usage)

        # 3. Build the matrix ------------------------------------------------
        matrix_model = MatrixModel(rows=rows)
        now = datetime.now(tz=UTC)
        matrix = Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="matrix",
            label="literature-matrix",
            content=matrix_model.model_dump_json(),
            mime_type="application/json",
            produced_by="critic",
            created_at=now,
        )

        # 4. Synthesize the narrative ----------------------------------------
        narrative = await self._synthesize(rows, payload.feedback, payload.focus, usage)
        # FR-2.2: the summary opens with the rendered Markdown comparison table,
        # followed by the narrative review.
        matrix_table = self._render_matrix_markdown(rows)
        summary_content = f"## Comparison Matrix\n\n{matrix_table}\n\n## Synthesis\n\n{narrative}"
        summary = Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="summary",
            label="literature-summary",
            content=summary_content,
            mime_type="text/markdown",
            produced_by="critic",
            created_at=now,
        )

        _log.info(
            "critic_done",
            project_id=str(project_id),
            paper_count=len(rows),
            llm_calls=usage.llm_calls,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
        )
        return CriticOutput(matrix=matrix, summary=summary, usage=usage)

    @staticmethod
    def _accumulate(usage: CriticUsage, telemetry: dict[str, object]) -> None:
        """Fold one LLM call's telemetry dict into the running usage total."""
        usage.llm_calls += 1
        tin = telemetry.get("tokens_in")
        if isinstance(tin, int):
            usage.tokens_in += tin
        tout = telemetry.get("tokens_out")
        if isinstance(tout, int):
            usage.tokens_out += tout
        cost = telemetry.get("cost_usd")
        if isinstance(cost, int | float):
            usage.cost_usd = (usage.cost_usd or 0.0) + float(cost)
        model = telemetry.get("model")
        if usage.model is None and isinstance(model, str):
            usage.model = model

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _render_matrix_markdown(rows: list[PaperExtraction]) -> str:
        """Render the comparison matrix as a Markdown table (BRD FR-2.2).

        FR-2.2 asks for the matrix as "structured JSON + rendered Markdown
        table". The JSON lives in the `matrix` artifact; this table is prepended
        to the `summary` artifact so the narrative review opens with an at-a-
        glance comparison grid.
        """

        def cell(text: str) -> str:
            # Escape pipes and collapse newlines so the table stays well-formed.
            return text.replace("|", "\\|").replace("\n", " ").strip() or "—"

        header = (
            "| Paper | Problem | Method | Dataset | Key findings | Limitations |\n"
            "| --- | --- | --- | --- | --- | --- |"
        )
        lines = [header]
        for r in rows:
            if r.extraction_failed:
                lines.append(f"| `{cell(r.citation_key)}` | _extraction failed_ |  |  |  |  |")
                continue
            lines.append(
                f"| `{cell(r.citation_key)}` | {cell(r.problem)} | {cell(r.method)} "
                f"| {cell(r.dataset)} | {cell(r.key_findings)} | {cell(r.limitations)} |"
            )
        return "\n".join(lines)

    @staticmethod
    def _resolve_project_id(papers: list[Paper]) -> UUID:
        for paper in papers:
            if paper.project_id is not None:
                return paper.project_id
        return uuid4()

    async def _embed(self, project_id: UUID, papers: list[Paper]) -> bool:
        """Embed paper abstracts into the vector store.

        Returns True if RAG context is available, False if the vector store
        is unreachable (the Critic then extracts from abstracts directly).
        """
        documents: list[dict[str, object]] = [
            {"id": p.citation_key, "text": p.abstract or p.title}
            for p in papers
            if (p.abstract or p.title)
        ]
        try:
            await self._vs.upsert(namespace=str(project_id), documents=documents)
            return True
        except VectorStoreUnavailableError as exc:
            _log.warning("critic_rag_unavailable", error_type=type(exc).__name__, error=str(exc))
            return False

    async def _extract_batch(
        self,
        project_id: UUID,
        papers: list[Paper],
        feedback: str | None,
        rag_available: bool,
        usage: CriticUsage,
    ) -> list[PaperExtraction]:
        """Extract attributes for every approved paper in a SINGLE LLM call.

        Cuts the Critic's extraction calls from O(N) papers to O(1), which
        is the difference between fitting inside the Gemini free-tier daily
        budget (20 calls/day) and blowing through it.

        Failure semantics preserve docs/agents/critic.md §Invariants — every
        approved paper appears in the returned list. The graceful-degradation
        ladder, from best to worst, is:

          1. LLM returns a clean object with N extractions  → all N papers OK.
          2. LLM returns an object missing some papers       → missing ones get
             ``extraction_failed=True`` rows; present ones flow through.
          3. LLM call raises OR response can't be parsed     → every paper gets
             an ``extraction_failed=True`` row carrying the same error string;
             the node does not crash, the user can reject + regenerate.
        """
        # Optional RAG context — one query covering the broad topic from the
        # first paper's title is enough for the batched call. With the
        # per-paper version we queried once per paper; here a single shared
        # block is fine because the prompt scope is already wider.
        rag_block = ""
        if rag_available and papers:
            try:
                # Hybrid (BM25 + dense + RRF + optional rerank) when enabled;
                # exactly the legacy dense top-3 when the flag is off. The
                # prompt below is unchanged — only the fetch mechanism differs.
                hits = await self._vs.hybrid_reranked_search(
                    namespace=str(project_id), query=papers[0].title
                )
                if hits:
                    # W1-A1: RAG snippets are chunked from the same poisoned-
                    # paper-text source the abstract came from — wrap in <rag>
                    # so a crafted PDF body can't issue instructions either.
                    snippets = " ".join(str(h.get("text", "")) for h in hits)
                    rag_block = f"Related context: {safe_tag('rag', snippets)}\n"
            except VectorStoreUnavailableError as exc:
                _log.warning(
                    "critic_rag_query_failed", error_type=type(exc).__name__, error=str(exc)
                )

        # W1-A1: wrap untrusted reviewer feedback so it can't override the
        # system instructions above.
        feedback_block = (
            f"Apply the following revision instruction: {safe_tag('reviewer_feedback', feedback)}\n\n"
            if feedback
            else ""
        )
        # Render the papers block — one <paper id="key"> tag per paper, with
        # title/abstract as escaped child tags. Keeps order so the model's
        # response ordering matches our internal list when we re-merge.
        papers_block_lines: list[str] = []
        for paper in papers:
            papers_block_lines.append(
                "---\n"
                f"citation_key: {xml_escape(paper.citation_key)}\n"
                + safe_tag(
                    "paper",
                    safe_tag("title", paper.title)
                    + safe_tag("abstract", paper.abstract or "(no abstract available)"),
                    attrs={"id": paper.citation_key},
                    raw=True,
                )
                + "\n"
            )
        papers_block = "".join(papers_block_lines)

        prompt = _BATCH_EXTRACTION_PROMPT_TEMPLATE.format(
            paper_count=len(papers),
            feedback_block=feedback_block,
            papers_block=papers_block,
            rag_block=rag_block,
            system_anchor=SYSTEM_ANCHOR,
        )

        try:
            from google.genai import types as genai_types

            config = genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_BatchExtractionsEnvelope,
            )
            text, telemetry = await self._llm.complete(prompt, config=config)
            self._accumulate(usage, telemetry)
            envelope = _BatchExtractionsEnvelope.model_validate_json(text)
            received_by_key: dict[str, PaperExtraction] = {
                row.citation_key: row for row in envelope.extractions if row.citation_key
            }
        except Exception as exc:  # any failure → every paper gets an error row
            _log.warning(
                "critic_batch_extraction_failed", error_type=type(exc).__name__, error=str(exc)
            )
            return [
                PaperExtraction(
                    citation_key=paper.citation_key,
                    extraction_failed=True,
                    error=str(exc),
                )
                for paper in papers
            ]

        # Stitch the response back to the approved-pool order. Papers that
        # the LLM forgot to extract get an extraction_failed row so the
        # matrix invariant (one row per approved paper) holds.
        result: list[PaperExtraction] = []
        for paper in papers:
            row = received_by_key.get(paper.citation_key)
            if row is None:
                result.append(
                    PaperExtraction(
                        citation_key=paper.citation_key,
                        extraction_failed=True,
                        error="missing from batched LLM response",
                    )
                )
            else:
                # Ensure citation_key matches the approved paper — the LLM is
                # asked to echo our key, but be defensive about drift.
                result.append(
                    PaperExtraction(
                        citation_key=paper.citation_key,
                        problem=row.problem,
                        method=row.method,
                        dataset=row.dataset,
                        key_findings=row.key_findings,
                        limitations=row.limitations,
                    )
                )
        return result

    async def _extract(
        self,
        project_id: UUID,
        paper: Paper,
        feedback: str | None,
        rag_available: bool,
        usage: CriticUsage,
    ) -> PaperExtraction:
        """Run the structured extraction LLM call for one paper.

        On LLM failure the row is marked `extraction_failed` with the error —
        the node does not fail (docs/agents/critic.md §Failure modes).
        """
        rag_block = ""
        if rag_available:
            try:
                hits = await self._vs.hybrid_reranked_search(
                    namespace=str(project_id), query=paper.title
                )
                if hits:
                    # W1-A1: RAG snippets are chunked from the same poisoned-
                    # paper-text source the abstract came from — wrap in <rag>
                    # so a crafted PDF body can't issue instructions either.
                    snippets = " ".join(str(h.get("text", "")) for h in hits)
                    rag_block = f"Related context: {safe_tag('rag', snippets)}\n"
            except VectorStoreUnavailableError as exc:
                _log.warning(
                    "critic_rag_query_failed", error_type=type(exc).__name__, error=str(exc)
                )

        # W1-A1: wrap untrusted reviewer feedback + paper title/abstract.
        feedback_block = (
            f"Apply the following revision instruction: {safe_tag('reviewer_feedback', feedback)}\n\n"
            if feedback
            else ""
        )
        paper_block = safe_tag(
            "paper",
            safe_tag("title", paper.title)
            + safe_tag("abstract", paper.abstract or "(no abstract available)"),
            attrs={"id": paper.citation_key},
            raw=True,
        )
        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
            feedback_block=feedback_block,
            citation_key=xml_escape(paper.citation_key),
            paper_block=paper_block,
            rag_block=rag_block,
            system_anchor=SYSTEM_ANCHOR,
        )
        try:
            from google.genai import types as genai_types

            config = genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PaperExtraction,
            )
            text, telemetry = await self._llm.complete(prompt, config=config)
            self._accumulate(usage, telemetry)
            data = json.loads(text)
            return PaperExtraction(
                citation_key=paper.citation_key,
                problem=str(data.get("problem", "")),
                method=str(data.get("method", "")),
                dataset=str(data.get("dataset", "")),
                key_findings=str(data.get("key_findings", "")),
                limitations=str(data.get("limitations", "")),
            )
        except Exception as exc:  # one paper must not fail the whole node
            _log.warning(
                "critic_extraction_failed",
                citation_key=paper.citation_key,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return PaperExtraction(
                citation_key=paper.citation_key,
                extraction_failed=True,
                error=str(exc),
            )

    async def _synthesize(
        self,
        rows: list[PaperExtraction],
        feedback: str | None,
        focus: str | None,
        usage: CriticUsage,
    ) -> str:
        """Generate the narrative summary from the per-paper extractions."""
        # W1-A1 follow-up: feedback and focus are untrusted reviewer input —
        # wrap them in <reviewer_feedback> / <focus> tags so a crafted note
        # can't override the system instructions above. The SYSTEM_ANCHOR at
        # the END of the prompt template reinforces the boundary.
        feedback_block = (
            f"Apply the following revision instruction: {safe_tag('reviewer_feedback', feedback)}\n\n"
            if feedback
            else ""
        )
        focus_block = f"Focus the synthesis on: {safe_tag('focus', focus)}\n\n" if focus else ""
        extractions_json = json.dumps([r.model_dump() for r in rows], indent=2)
        prompt = _SYNTHESIS_PROMPT_TEMPLATE.format(
            feedback_block=feedback_block,
            focus_block=focus_block,
            extractions_json=extractions_json,
            system_anchor=SYSTEM_ANCHOR,
        )
        try:
            text, telemetry = await self._llm.complete(prompt)
            self._accumulate(usage, telemetry)
            return text or "## Synthesis\n\n(No narrative produced.)"
        except Exception as exc:  # degrade gracefully on synthesis failure
            _log.warning("critic_synthesis_failed", error_type=type(exc).__name__, error=str(exc))
            return f"## Synthesis\n\nNarrative generation failed: {exc}"

    @staticmethod
    def _error_output(message: str) -> CriticOutput:
        # matrix and summary must be *distinct* Artifact rows: _persist_artifacts
        # de-dupes on the primary key with ON CONFLICT DO NOTHING, so reusing one
        # Artifact (same id) for both slots would silently drop one of them on
        # persist (PR #5 finding). Build two rows with their own ids.
        now = datetime.now(tz=UTC)
        pid = uuid4()

        def _err(label: str) -> Artifact:
            return Artifact(
                id=uuid4(),
                project_id=pid,
                kind="log",
                label=label,
                content=message,
                mime_type="text/plain",
                produced_by="critic",
                created_at=now,
            )

        return CriticOutput(
            matrix=_err("critic-error-matrix"), summary=_err("critic-error-summary")
        )
