"""The Scribe agent — section drafting. See SPEC.md §6.4 and docs/agents/scribe.md.

Given the approved paper pool, a target section name, and any prior approved
sections, the Scribe:
  1. Pulls RAG context for the section from the per-project vector store.
  2. Builds a section-specific prompt that lists the *only* citation keys the
     model is allowed to use.
  3. Calls the LLM.
  4. Validates every `[@key]` reference against the approved pool.
     - First failure → re-prompts with the offenders called out (one retry).
     - Second failure → returns the draft anyway, flagging the unknown keys
       with an ``INVALID:`` prefix so the frontend can surface a warning.

Failure semantics intentionally mirror docs/agents/scribe.md: a single auto-retry,
then surface the issue rather than hard-failing the node. Empty approved pool
and `output_format == "latex"` (v0.2) are explicit errors.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.agents._prompt_safety import SYSTEM_ANCHOR, safe_tag, xml_escape
from app.agents.base import Agent
from app.models.schemas import Artifact, Paper, SectionName
from app.services.llm import LLMGateway, get_llm_gateway
from app.services.vector_store import (
    VectorStore,
    VectorStoreUnavailableError,
    get_vector_store,
)
from app.utils.logging import get_logger

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# System-level rules every section must obey.
#
# This block is prepended to every section prompt. It encodes BRD §10
# risk mitigations directly into the LLM instruction:
#
#   - **No "human cosplay"**: the Scribe is NOT a human grad student running
#     a manual literature search. It is documenting an *agentic* pipeline.
#     Fabricated descriptions of IEEE Xplore queries, ACM searches, manual
#     screening stages etc. are prohibited.
#   - **Academic hedging**: with a small approved_pool (typical demo runs
#     have 3-10 papers), strong claims like "the literature proves" or
#     "the headline finding is" are not supportable. The model must use
#     hedged language and disclose the pool size where relevant.
#   - **Citation invariant** (already enforced post-hoc by validate_citations,
#     but stated here so the model produces fewer offenders on the first try).
# ---------------------------------------------------------------------------
_ANTI_COSPLAY_RULES = """\
You are the Scribe agent of an AI literature-review pipeline. You are NOT
a human researcher and you must NOT pretend to be one. Hard rules:

1. **Describe only what the pipeline actually did.** This run was performed
   by automated agents: a Librarian agent queried open APIs, a human user
   approved a subset of returned papers, and a Critic agent produced a
   structured comparison. Do NOT invent manual search steps (e.g. "I
   searched IEEE Xplore using boolean operators", "I screened titles and
   abstracts in three stages", "we used a structured data-extraction
   template"). The actual workflow telemetry is supplied below — use it
   verbatim and only when describing how the review pool was assembled.

2. **Cite ONLY from the approved BibTeX keys provided below.** Do not
   invent keys, do not use DOIs or URLs in the body of the prose, do not
   fabricate paper titles or author names. Citation markers must use the
   `[@key]` syntax. The approved pool is small — typical runs have only a
   few papers — and your reasoning must reflect that.

3. **Use academic hedging proportional to the pool size.** With a small
   sample (under ~10 papers), avoid definitive claims about "the field" or
   "the literature". Prefer phrases like "within this small reviewed
   sample", "the surveyed papers suggest", "based on the {pool_size} papers
   in this review", "tentatively", "appears to". Avoid: "proves",
   "definitively shows", "the headline finding is that ...", "it is now
   established that ...". Discuss limitations of the sample explicitly
   when summarising or concluding.

4. **Reference the live telemetry where it makes the prose honest.** The
   facts about which sources were queried, how many candidates were
   returned, and how many the user approved are below. Use them in the
   Methodology section. Do not invent additional numbers.

Workflow telemetry for this run (use verbatim — do NOT change the numbers):
{telemetry_block}
"""


# Per-section prompt prefix. Body of the prompt (pool + RAG + feedback) is
# appended below. Each prefix encodes the section's BRD-mandated character —
# abstract = short, methodology = procedural, etc.
_SECTION_PREFIXES: dict[str, str] = {
    "abstract": (
        "Write the **abstract** of a literature-review manuscript. Target 150-250 "
        "words. State the topic, that this is a small AI-assisted review of "
        "{pool_size} papers, the methodological groupings observed, and a hedged "
        "summary of what the surveyed papers indicate. No citations needed in "
        "the abstract."
    ),
    "introduction": (
        "Write the **introduction** section. 2-4 paragraphs. Motivate the topic, "
        "state the question that the {pool_size} surveyed papers collectively "
        "speak to, and preview the structure of the review. Disclose that the "
        "pool is small and AI-assembled rather than implying an exhaustive "
        "field-wide survey."
    ),
    "related_work": (
        "Write the **related_work** section. Cluster the cited papers by "
        "methodological approach (use 2-4 clusters; do NOT invent more clusters "
        "than the pool supports). Within each cluster, compare and contrast the "
        "papers. Every claim attributing a result to a paper must carry a "
        "`[@citation_key]` marker. With only {pool_size} papers, prefer "
        "comparative observations over sweeping generalisations."
    ),
    "methodology": (
        "Write the **methodology** section. This section MUST describe the "
        "*actual* agentic pipeline that produced this review. Use the workflow "
        "telemetry above verbatim. Structure as:\n"
        "  - Source selection: which APIs the Librarian agent queried (from "
        "    `sources_queried`).\n"
        "  - Query expansion: that the Librarian expanded the seed query into "
        "    the variants listed in `expanded_queries`.\n"
        "  - Candidate generation: that the pipeline returned "
        "    `candidate_count` candidate papers after deduplication and "
        "    ranking.\n"
        "  - Human review: that the user approved `approved_count` of the "
        "    candidates as the working pool (HITL gate).\n"
        "  - Synthesis: that a Critic agent extracted structured attributes "
        "    per paper and the Scribe agent (this agent) wrote the prose, with "
        "    every section gated by a human approval step.\n"
        "Do NOT fabricate manual database searches, manual screening stages, "
        "or structured human extraction templates."
    ),
    "results": (
        "Write the **results** section. Summarise what the {pool_size} surveyed "
        "papers collectively report: trends, agreements, contradictions. Use "
        "citation keys for every specific claim. Hedge in proportion to the "
        "pool size — with so few papers, framing observations as 'within this "
        "small sample' is more honest than generalising to the broader field."
    ),
    "discussion": (
        "Write the **discussion** section. Interpret the observations: what is "
        "consistent across the surveyed papers, what is contested, what is "
        "missing from the pool. Use citation keys for specific claims. Mark "
        "speculation explicitly (e.g. 'we speculate', 'one possibility is'). "
        "Acknowledge that conclusions drawn from {pool_size} papers cannot "
        "establish broad consensus in the field."
    ),
    "conclusion": (
        "Write the **conclusion** section. 1-2 paragraphs. Restate the hedged "
        "summary from this small AI-assisted review of {pool_size} papers, and "
        "propose two or three research directions implied by gaps in the "
        "surveyed pool. Do not claim the review covers the whole field."
    ),
}


_PROMPT_TEMPLATE = """\
{anti_cosplay}

{section_prefix}

Cite ONLY from the following BibTeX keys (use them as `[@key]`). Do not invent
keys, do not use DOIs or URLs in the body:
{approved_keys}

Approved-pool abstracts (for grounding):
{pool_block}
{rag_block}{prior_block}{feedback_block}
Output Markdown only. Begin with a `## {section_title}` heading.{system_anchor}"""


_CITATION_RE = re.compile(r"\[@([A-Za-z0-9_\-:.]+)\]")


def _format_telemetry_block(telemetry: dict[str, Any], pool_size: int) -> str:
    """Render workflow_telemetry as plain key:value text for the LLM prompt.

    Falls back to a minimal block when the graph hasn't supplied telemetry
    (older tests, stubs, etc.) so the Scribe still has something honest to
    say in the Methodology section.
    """
    if not telemetry:
        return f"- approved_count: {pool_size}\n- (no further telemetry recorded for this run)"
    rows: list[str] = []
    # Order matters — keep the most user-relevant first.
    for key in (
        "sources_queried",
        "sources_with_hits",
        "expanded_queries",
        "arxiv_categories",
        "candidate_count",
        "approved_count",
        "fulltext_ingested",
        "rag_available",
        "discovery_started_at",
        "discovery_finished_at",
        "synthesis_started_at",
        "synthesis_finished_at",
    ):
        if key in telemetry:
            rows.append(f"- {key}: {telemetry[key]}")
    # Anything else the graph stashed — append at the end so future
    # telemetry additions show up without prompt changes.
    for k, v in telemetry.items():
        if k not in {
            "sources_queried",
            "sources_with_hits",
            "expanded_queries",
            "arxiv_categories",
            "candidate_count",
            "approved_count",
            "fulltext_ingested",
            "rag_available",
            "discovery_started_at",
            "discovery_finished_at",
            "synthesis_started_at",
            "synthesis_finished_at",
        }:
            rows.append(f"- {k}: {v}")
    return "\n".join(rows)


def _extract_cited_keys(content: str) -> list[str]:
    """Return the citation keys appearing in `[@key]` markers, preserving
    first-occurrence order and deduplicating."""
    seen: dict[str, None] = {}
    for match in _CITATION_RE.finditer(content):
        key = match.group(1)
        if key not in seen:
            seen[key] = None
    return list(seen.keys())


class ScribeInput(BaseModel):
    section: SectionName
    approved_pool: list[Paper]
    prior_sections: list[Artifact]
    output_format: Literal["markdown", "latex"] = "markdown"
    feedback: str | None = None
    # Workflow telemetry injected by node_draft_section so the Methodology
    # section documents the *actual* agentic pipeline (BRD §10 risk
    # mitigation — no fabricated human literature-review process).
    # Shape matches GraphState.workflow_telemetry. Optional because legacy
    # callers (and tests of the citation validator) may not set it.
    workflow_telemetry: dict[str, Any] = Field(default_factory=dict)


class ScribeUsage(BaseModel):
    """Token + cost rollup for one Scribe run (BRD FR-3.3 / §4.3)."""

    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None
    llm_calls: int = 0


class ScribeOutput(BaseModel):
    section: Artifact
    # Keys *intended* to be cited. Offenders not in the approved pool after the
    # retry are surfaced here with an "INVALID:" prefix so the frontend can
    # render a warning chip beside the approve button.
    cited_keys: list[str]
    usage: ScribeUsage = ScribeUsage()


class Scribe(Agent[ScribeInput, ScribeOutput]):
    name = "scribe"

    def __init__(
        self,
        llm: LLMGateway | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        # Dependencies are injectable for testing; default to the singletons
        # (matches the Critic pattern).
        self._llm = llm if llm is not None else get_llm_gateway()
        self._vs = vector_store if vector_store is not None else get_vector_store()

    async def run(self, payload: ScribeInput) -> ScribeOutput:
        if payload.output_format == "latex":
            # BRD §8: LaTeX is v0.2. Fail fast rather than emit unrendered raw text.
            raise NotImplementedError(
                "LaTeX output is scheduled for v0.2; Phase 4 ships Markdown only."
            )
        if not payload.approved_pool:
            # The engine should never route here with an empty pool.
            raise ValueError("Scribe requires a non-empty approved_pool.")

        approved_keys = sorted({p.citation_key for p in payload.approved_pool})
        usage = ScribeUsage(model=getattr(self._llm, "model_name", None))

        project_id = self._resolve_project_id(payload.approved_pool)
        rag_context = await self._fetch_rag_context(project_id, payload)

        first_prompt = self._build_prompt(payload, approved_keys, rag_context, feedback=None)
        first_text, first_telemetry = await self._llm.complete(first_prompt)
        self._accumulate(usage, first_telemetry)

        cited = _extract_cited_keys(first_text)
        offenders = sorted(set(cited) - set(approved_keys))

        if not offenders:
            return self._build_output(payload, project_id, first_text, cited, usage)

        # One automatic retry with the validation error injected as feedback.
        retry_feedback = (
            f"Your previous draft cited the following keys that are NOT in the "
            f"approved pool: {offenders}. Re-write the section using ONLY these "
            f"approved citation keys: {approved_keys}."
        )
        retry_prompt = self._build_prompt(
            payload, approved_keys, rag_context, feedback=retry_feedback
        )
        retry_text, retry_telemetry = await self._llm.complete(retry_prompt)
        self._accumulate(usage, retry_telemetry)

        retry_cited = _extract_cited_keys(retry_text)
        retry_offenders = sorted(set(retry_cited) - set(approved_keys))

        if not retry_offenders:
            return self._build_output(payload, project_id, retry_text, retry_cited, usage)

        # Second failure — surface the offenders but still return the draft so
        # the user can override or reject through the gate.
        _log.warning(
            "scribe_invalid_citations_after_retry",
            section=payload.section,
            offenders=retry_offenders,
        )
        flagged: list[str] = list(retry_cited)
        for off in retry_offenders:
            flagged.append(f"INVALID:{off}")
        return self._build_output(payload, project_id, retry_text, flagged, usage)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def validate_citations(cited: list[str], approved_pool: list[Paper]) -> set[str]:
        """Return the set of cited keys that are NOT in the approved pool.

        Kept as a static helper so external callers (tests, future linters) can
        validate freeform markdown without instantiating the agent.
        """
        approved = {p.citation_key for p in approved_pool}
        return set(cited) - approved

    @staticmethod
    def _resolve_project_id(papers: list[Paper]) -> UUID:
        """Pick the (single) project_id from a non-empty approved pool.

        Every Paper persisted to the DB carries a non-null project_id (the
        Librarian stamps it before _persist_candidates). If the pool is
        somehow empty of stamped papers, generating a random uuid4() would
        silently route Chroma chunks into a namespace nobody owns. Raise
        loudly instead so the caller (graph node) surfaces a real error.
        """
        for p in papers:
            if p.project_id is not None:
                return p.project_id
        raise ValueError(
            "Scribe._resolve_project_id: approved_pool has no Paper with project_id; "
            "every persisted paper must carry a project_id (Librarian invariant)"
        )

    async def _fetch_rag_context(self, project_id: UUID, payload: ScribeInput) -> str:
        """Best-effort RAG. A vector-store outage is non-fatal — Scribe falls
        back to the approved-pool abstracts only (same posture as the Critic)."""
        try:
            query = f"{payload.section} {payload.feedback or ''}".strip()
            hits = await self._vs.query(str(project_id), query, k=5)
        except VectorStoreUnavailableError:
            _log.warning("scribe_rag_unavailable", project_id=str(project_id))
            return ""
        snippets: list[str] = []
        for h in hits:
            text = str(h.get("text") or "").strip()
            if text:
                snippets.append(f"- {text[:500]}")
        return "\n".join(snippets)

    @staticmethod
    def _build_prompt(
        payload: ScribeInput,
        approved_keys: list[str],
        rag_context: str,
        feedback: str | None,
    ) -> str:
        pool_size = len(payload.approved_pool)
        # Section prefix gets pool_size substituted so prompts can say "the
        # {pool_size} surveyed papers" honestly.
        section_prefix = _SECTION_PREFIXES.get(
            payload.section, f"Write the **{payload.section}** section."
        ).replace("{pool_size}", str(pool_size))
        # Telemetry block rendered as plain key-value text — easier for the
        # LLM to copy verbatim into the Methodology section than nested JSON.
        anti_cosplay = _ANTI_COSPLAY_RULES.format(
            pool_size=pool_size,
            telemetry_block=_format_telemetry_block(payload.workflow_telemetry, pool_size),
        )
        # W1-A1: every untrusted string is wrapped in an XML tag with HTML
        # entities escaped, so a crafted abstract or feedback message cannot
        # break out and override the system instructions above.
        pool_block = "\n".join(
            f"[@{p.citation_key}] "
            + safe_tag(
                "paper",
                # raw=True: the body here is already-built safe_tag output,
                # do not re-escape its angle brackets.
                f"{safe_tag('title', p.title)} {safe_tag('abstract', p.abstract or '(no abstract)')}",
                attrs={"id": p.citation_key},
                raw=True,
            )
            for p in payload.approved_pool
        )
        rag_block = (
            f"\nRetrieved passages:\n{safe_tag('rag', rag_context)}\n" if rag_context else ""
        )
        prior_block = ""
        if payload.prior_sections:
            prior_block = (
                "\nPreviously approved sections (read-only context):\n"
                + "\n".join(
                    safe_tag(
                        "prior_section",
                        a.content[:1200],
                        attrs={"label": a.label},
                    )
                    for a in payload.prior_sections
                )
                + "\n"
            )
        feedback_block = ""
        external_feedback = (feedback or payload.feedback or "").strip()
        if external_feedback:
            feedback_block = (
                f"\nRevision instruction: {safe_tag('reviewer_feedback', external_feedback)}\n"
            )
        return _PROMPT_TEMPLATE.format(
            anti_cosplay=anti_cosplay,
            section_prefix=section_prefix,
            # Citation keys are pool-controlled (sourced from PaperRow that
            # the Librarian generated); still escape defensively in case a
            # future Librarian change loosens the generator.
            approved_keys=", ".join(f"`{xml_escape(k)}`" for k in approved_keys),
            pool_block=pool_block,
            rag_block=rag_block,
            prior_block=prior_block,
            feedback_block=feedback_block,
            section_title=payload.section.replace("_", " ").title(),
            system_anchor=SYSTEM_ANCHOR,
        )

    @staticmethod
    def _accumulate(usage: ScribeUsage, telemetry: dict[str, object]) -> None:
        usage.llm_calls += 1
        ti = telemetry.get("tokens_in")
        to = telemetry.get("tokens_out")
        if isinstance(ti, int):
            usage.tokens_in += ti
        if isinstance(to, int):
            usage.tokens_out += to
        cost = telemetry.get("cost_usd")
        if isinstance(cost, (int, float)):
            usage.cost_usd = (usage.cost_usd or 0.0) + float(cost)

    @staticmethod
    def _build_output(
        payload: ScribeInput,
        project_id: UUID,
        content: str,
        cited_keys: list[str],
        usage: ScribeUsage,
    ) -> ScribeOutput:
        artifact = Artifact(
            id=uuid4(),
            project_id=project_id,
            kind="section",
            label=payload.section,
            content=content,
            mime_type="text/markdown",
            produced_by="scribe",
            created_at=datetime.now(tz=UTC),
        )
        return ScribeOutput(section=artifact, cited_keys=cited_keys, usage=usage)
