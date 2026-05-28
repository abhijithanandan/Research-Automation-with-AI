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
Title: {title}
Abstract: {abstract}
{rag_block}
Return JSON with keys: citation_key, problem, method, dataset,
key_findings, limitations.
"""

_SYNTHESIS_PROMPT_TEMPLATE = """\
You are a research critic writing a literature synthesis (a narrative
review). Using the per-paper extractions below, write a 3-6 paragraph
narrative grouped by methodological cluster. Reference papers only by
their citation_key. Do not invent papers.

{feedback_block}{focus_block}Per-paper extractions (JSON):
{extractions_json}

Write the narrative synthesis in Markdown.
"""


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

        # 2. Extract per-paper attributes ------------------------------------
        rows = [
            await self._extract(project_id, paper, payload.feedback, rag_available, usage)
            for paper in papers
        ]

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
            _log.warning("critic_rag_unavailable", error=str(exc))
            return False

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
                hits = await self._vs.query(namespace=str(project_id), query=paper.title, k=3)
                if hits:
                    snippets = " ".join(str(h.get("text", "")) for h in hits)
                    rag_block = f"Related context: {snippets}\n"
            except VectorStoreUnavailableError as exc:
                _log.warning("critic_rag_query_failed", error=str(exc))

        feedback_block = (
            f"Apply the following revision instruction: {feedback}\n\n" if feedback else ""
        )
        prompt = _EXTRACTION_PROMPT_TEMPLATE.format(
            feedback_block=feedback_block,
            citation_key=paper.citation_key,
            title=paper.title,
            abstract=paper.abstract or "(no abstract available)",
            rag_block=rag_block,
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
        feedback_block = (
            f"Apply the following revision instruction: {feedback}\n\n" if feedback else ""
        )
        focus_block = f"Focus the synthesis on: {focus}\n\n" if focus else ""
        extractions_json = json.dumps([r.model_dump() for r in rows], indent=2)
        prompt = _SYNTHESIS_PROMPT_TEMPLATE.format(
            feedback_block=feedback_block,
            focus_block=focus_block,
            extractions_json=extractions_json,
        )
        try:
            text, telemetry = await self._llm.complete(prompt)
            self._accumulate(usage, telemetry)
            return text or "## Synthesis\n\n(No narrative produced.)"
        except Exception as exc:  # degrade gracefully on synthesis failure
            _log.warning("critic_synthesis_failed", error=str(exc))
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
