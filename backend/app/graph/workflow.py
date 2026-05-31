"""LangGraph workflow builder. See SPEC.md §5 for the node/edge contract.

Phase 1 implements:
  - `discover` node  → calls Librarian, writes candidates to state.
  - `await_pool_approval` gate — the HITL gate for Phase 1 approval.

Phase 2 implements:
  - `synthesize` node → calls Critic, writes matrix + summary to state.
  - `await_synthesis_approval` gate — the HITL gate for Phase 2 approval.

Phase 4 implements:
  - `draft_section` node → calls Scribe once per canonical section (abstract
    → introduction → related_work → methodology → results → discussion →
    conclusion).
  - `await_section_approval` gate — HITL gate that fires once per section.
  - `assemble` node → concatenates approved drafts into a single
    `kind="manuscript"` artifact in canonical order.

Approval gates use LangGraph's `interrupt()` — the graph pauses at the
interrupt point until an external `Command(resume=…)` is issued by the
workflow REST endpoint.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app.agents.critic import Critic, CriticInput
from app.agents.librarian import Librarian, LibrarianInput
from app.agents.scribe import Scribe, ScribeInput
from app.graph.state import GraphState
from app.models.schemas import Artifact, Paper, Phase
from app.utils.logging import get_logger

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Node name constants — always reference these, never raw strings
# ---------------------------------------------------------------------------
NODE_DISCOVER = "discover"
NODE_AWAIT_POOL = "await_pool_approval"
NODE_SYNTHESIZE = "synthesize"
NODE_AWAIT_SYNTHESIS = "await_synthesis_approval"
NODE_ANALYZE = "analyze"  # v0.2
NODE_AWAIT_ANALYSIS = "await_analysis_approval"  # v0.2
NODE_DRAFT = "draft_section"
NODE_AWAIT_SECTION = "await_section_approval"
NODE_ASSEMBLE = "assemble"
NODE_DONE = "done"


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------


async def node_discover(state: GraphState) -> GraphState:
    """Run the Librarian agent and populate `candidates` in the state.

    Also captures discovery-phase telemetry (sources queried, sources that
    actually returned papers, candidate count, ISO timestamps) into
    ``workflow_telemetry`` so the Scribe can document the real agentic
    workflow in the manuscript's Methodology section (BRD §10 risk
    mitigation — no fabricated human literature-review processes).
    """
    _log.info("node_discover_start", project_id=str(state.get("project_id")))
    discovery_started_at = datetime.now(tz=UTC).isoformat()

    librarian = Librarian()
    result = await librarian.run(
        LibrarianInput(
            seed_query=state.get("seed_query", ""),
            project_id=state.get("project_id"),
        )
    )

    # Per-source counts from the returned candidates — this is the *post-dedup*
    # picture, which is exactly what we want the Methodology to describe.
    sources_with_hits: list[str] = sorted({p.source for p in result.candidates})
    # The full set of sources the DiscoveryService fans out to today. Kept as
    # an explicit list so the Methodology can be precise about *what was
    # queried* even when a source returned zero (e.g., 429-throttled).
    sources_queried: list[str] = [
        "semantic_scholar",
        "arxiv",
        "crossref",
        "core",
        "europe_pmc",
    ]

    discovery_finished_at = datetime.now(tz=UTC).isoformat()
    telemetry: dict[str, Any] = dict(state.get("workflow_telemetry") or {})
    telemetry.update(
        {
            "sources_queried": sources_queried,
            "sources_with_hits": sources_with_hits,
            "expanded_queries": result.expanded_queries,
            "arxiv_categories": result.arxiv_categories,
            "candidate_count": len(result.candidates),
            "discovery_started_at": discovery_started_at,
            "discovery_finished_at": discovery_finished_at,
        }
    )

    _log.info(
        "node_discover_done",
        candidate_count=len(result.candidates),
        sources_with_hits=sources_with_hits,
    )

    return {
        **state,
        "phase": Phase.DISCOVERY,
        "candidates": [p.model_dump(mode="json") for p in result.candidates],
        "expanded_queries": result.expanded_queries,
        "workflow_telemetry": telemetry,
        # Surface query-expansion LLM usage so _run_graph can write it to
        # audit_log and apply the cost cap (NFR-5) before requesting approval.
        "discovery_usage": result.usage,
        "awaiting_approval": False,  # gate node sets this
    }


async def node_await_pool_approval(state: GraphState) -> GraphState:
    """HITL gate for Phase 1.

    Persists a checkpoint, then issues an interrupt. The graph is suspended
    here until the `/workflow/approve` (or /reject) endpoint resumes it with
    a `Command`. This satisfies SPEC.md §5.3 gate invariants — the checkpoint
    is written *before* the interrupt is emitted.
    """
    _log.info("gate_pool_approval_waiting", project_id=str(state.get("project_id")))

    # `interrupt()` raises `GraphInterrupt` internally — LangGraph persists
    # the checkpoint before raising, then re-enters this node with the
    # resume value when the graph is commanded to continue.
    approval = interrupt(
        {
            "phase": Phase.DISCOVERY,
            "message": "Review and approve the candidate paper pool.",
        }
    )

    # On resume, `approval` carries the action string: "approve" | "reject".
    # Safer default: anything other than the literal "approve" is treated as
    # reject. Previously any non-"reject" string (including None, "", garbage)
    # silently advanced the graph — audit finding #6.
    if approval == "approve":
        _log.info("gate_pool_approved", project_id=str(state.get("project_id")))
        return {**state, "awaiting_approval": False, "pool_approval": "approve"}
    if approval != "reject":
        _log.warning(
            "gate_pool_unknown_resume",
            project_id=str(state.get("project_id")),
            value=str(approval)[:64],
        )
    _log.info("gate_pool_rejected", project_id=str(state.get("project_id")))
    return {**state, "awaiting_approval": False, "pool_approval": "reject"}


async def node_synthesize(state: GraphState) -> GraphState:
    """Run the Critic agent over the approved pool — Phase 2 synthesis.

    Before invoking the Critic, the full-text fetcher downloads open-access
    PDFs (Semantic Scholar / arXiv / known OA mirrors), parses them with
    ``pypdf``, chunks the text and pushes chunks into the project's ChromaDB
    namespace. The Critic's existing ``vector_store.query`` calls then surface
    real paper content as RAG context instead of just abstracts (BRD FR-1.2).

    Full-text ingestion is best-effort — any failure logs a warning and the
    Critic falls back to abstract-only extraction (matches the rest of the
    Phase 2 graceful-degradation contract).
    """
    _log.info("node_synthesize_start", project_id=str(state.get("project_id")))
    synthesis_started_at = datetime.now(tz=UTC).isoformat()

    approved_raw = state.get("approved_pool", [])
    approved_papers = [Paper(**d) for d in approved_raw]

    # Best-effort full-text ingestion → ChromaDB. Errors must not sink the run.
    # Step (a): Unpaywall enrichment — for any paper without a pdf_url that
    # carries a DOI, look up a legal OA PDF URL. This dramatically raises
    # full-text coverage for Crossref-only papers (they rarely come with PDFs).
    # Step (b): the fulltext fetcher downloads + parses + embeds chunks.
    project_id = state.get("project_id")
    fulltext_ingested = 0
    if project_id is not None and approved_papers:
        try:
            from app.services.unpaywall import get_unpaywall_enricher

            approved_papers = await get_unpaywall_enricher().enrich(approved_papers)
            resolved = sum(1 for p in approved_papers if p.pdf_url is not None)
            _log.info(
                "unpaywall_enrich_done",
                project_id=str(project_id),
                resolved=resolved,
                pool_size=len(approved_papers),
            )
        except Exception as exc:  # never fail synthesis because of Unpaywall
            _log.warning("unpaywall_enrich_skipped", error_type=type(exc).__name__, error=str(exc))

        try:
            from app.services.fulltext_fetcher import get_fulltext_fetcher
            from app.services.workflow import _emit as _emit_ws

            # W2-C1: emit a `fulltext_progress` WS event after each paper
            # finishes so the frontend can render a "N/M papers indexed" chip
            # during what used to be a silent ~120s wait.
            async def _on_progress(done: int, total: int) -> None:
                await _emit_ws(
                    project_id,
                    {"type": "fulltext_progress", "done": done, "total": total},
                )

            fulltext_ingested = await get_fulltext_fetcher().ingest(
                project_id, approved_papers, on_progress=_on_progress
            )
            _log.info(
                "fulltext_ingest_done",
                project_id=str(project_id),
                ingested=fulltext_ingested,
                pool_size=len(approved_papers),
            )
        except Exception as exc:  # never fail synthesis because of a PDF
            _log.warning("fulltext_ingest_skipped", error_type=type(exc).__name__, error=str(exc))

    critic = Critic()
    result = await critic.run(
        CriticInput(
            approved_papers=approved_papers,
            focus=None,
            feedback=state.get("last_feedback"),
        )
    )

    _log.info("node_synthesize_done", paper_count=len(approved_papers))
    synthesis_finished_at = datetime.now(tz=UTC).isoformat()

    # Telemetry update — the Scribe consumes this to write a truthful
    # Methodology section. `approved_count` and `fulltext_ingested` are the
    # two new facts; `rag_available` is True iff at least one PDF chunk
    # made it into the vector store this run.
    telemetry: dict[str, Any] = dict(state.get("workflow_telemetry") or {})
    telemetry.update(
        {
            "approved_count": len(approved_papers),
            "fulltext_ingested": fulltext_ingested,
            "rag_available": fulltext_ingested > 0,
            "synthesis_started_at": synthesis_started_at,
            "synthesis_finished_at": synthesis_finished_at,
        }
    )

    return {
        **state,
        "phase": Phase.SYNTHESIS,
        "matrix": result.matrix.model_dump(mode="json"),
        "summary": result.summary.model_dump(mode="json"),
        "synthesis_usage": result.usage.model_dump(mode="json"),
        "workflow_telemetry": telemetry,
        "awaiting_approval": False,  # gate node sets this
    }


async def node_await_synthesis_approval(state: GraphState) -> GraphState:
    """HITL gate for Phase 2.

    Mirrors `node_await_pool_approval`: issues an `interrupt()` so LangGraph
    persists the checkpoint and suspends the graph until the
    `/workflow/{approve|reject|override}` endpoint resumes it with a Command.

    On `override` (SPEC §5.2/§5.3) the human-edited artifact carried in
    `last_override` becomes the *canonical* output of the synthesis node — it
    replaces the Critic's `matrix` or `summary` in state so drafting consumes
    the human version, not the agent's.
    """
    _log.info("gate_synthesis_approval_waiting", project_id=str(state.get("project_id")))

    approval = interrupt(
        {
            "phase": Phase.SYNTHESIS,
            "message": "Review and approve the literature synthesis.",
        }
    )

    # Match the same defensive default as the pool gate — only the literal
    # "approve" passes; everything else (including unexpected resume values)
    # is treated as reject (audit finding #6).
    if approval != "approve":
        if approval != "reject":
            _log.warning(
                "gate_synthesis_unknown_resume",
                project_id=str(state.get("project_id")),
                value=str(approval)[:64],
            )
        _log.info("gate_synthesis_rejected", project_id=str(state.get("project_id")))
        return {**state, "awaiting_approval": False, "synthesis_approval": "reject"}

    # Override: a manually-edited artifact replaces the agent output as the
    # canonical synthesis result (SPEC §5.3 — manual_override semantics).
    override = state.get("last_override")
    new_state: GraphState = {
        **state,
        "awaiting_approval": False,
        "synthesis_approval": "approve",
    }
    if override is not None:
        kind = override.get("kind") if isinstance(override, dict) else None
        if kind == "summary":
            new_state["summary"] = override
            _log.info("gate_synthesis_override_summary", project_id=str(state.get("project_id")))
        elif kind == "matrix":
            new_state["matrix"] = override
            _log.info("gate_synthesis_override_matrix", project_id=str(state.get("project_id")))
        else:
            # Unknown kind — previously this dropped the override silently
            # (audit finding #5). Log loudly so it's visible in the audit
            # trail, then still clear the field so a later gate doesn't
            # re-consume the same payload.
            _log.warning(
                "gate_synthesis_override_unknown_kind",
                project_id=str(state.get("project_id")),
                kind=str(kind)[:64],
            )
        # Clear it so a later gate does not re-consume the same override.
        new_state["last_override"] = None
        return new_state

    _log.info("gate_synthesis_approved", project_id=str(state.get("project_id")))
    return new_state


# Canonical seven-section order — BRD §5.2 FR-2.4. The graph drafts these in
# this order; node_assemble re-orders the drafts list to this order before
# concatenation, so out-of-order rejects/overrides can't corrupt the manuscript.
_CANONICAL_SECTIONS: list[str] = [
    "abstract",
    "introduction",
    "related_work",
    "methodology",
    "results",
    "discussion",
    "conclusion",
]


async def node_draft_section(state: GraphState) -> GraphState:
    """Phase 4 — Scribe drafts one section, then the graph hits the gate.

    On a reject re-run, ``current_section`` is already set in state and
    ``sections_remaining`` still has the current section at the head — we
    re-draft *that* section and overwrite the previous draft entry.

    On the approve path, the service layer's approve_workflow has already
    shuffled ``current_section`` out of ``sections_remaining`` and into
    ``sections_done``; the head of ``sections_remaining`` is the next section
    to draft.
    """
    project_id = state.get("project_id")
    _log.info("node_draft_section_start", project_id=str(project_id))

    # Decide which section this iteration drafts.
    last_section = state.get("current_section")
    remaining = list(state.get("sections_remaining", []))
    if state.get("section_approval") == "reject" and last_section:
        # Reject re-runs the *current* section with the same remaining list
        # (the service layer left section_approval=='reject' on the resume).
        section = last_section
    elif remaining:
        section = remaining[0]
    else:
        # Defence-in-depth — caller should have routed to assemble instead.
        _log.warning("node_draft_section_no_remaining", project_id=str(project_id))
        return {**state, "phase": Phase.DRAFTING}

    approved_raw = state.get("approved_pool", [])
    approved_papers = [Paper(**d) for d in approved_raw]
    prior_sections_state = state.get("drafts", []) or []
    prior_artifacts = [
        Artifact(**d["artifact"]) for d in prior_sections_state if d.get("section") != section
    ]

    scribe = Scribe()
    _draft_start = time.perf_counter()
    result = await scribe.run(
        ScribeInput(
            section=section,  # type: ignore[arg-type]
            approved_pool=approved_papers,
            prior_sections=prior_artifacts,
            feedback=state.get("last_feedback"),
            workflow_telemetry=dict(state.get("workflow_telemetry") or {}),
        )
    )
    draft_ms = int((time.perf_counter() - _draft_start) * 1000)

    # Carry the per-section drafting latency (NFR-6 / §9 success metric) on the
    # usage dict so the section gate handler can write it to the
    # phase_4.section_ready audit row without a second timing source.
    usage_payload = result.usage.model_dump(mode="json")
    usage_payload["draft_ms"] = draft_ms

    # Replace any prior draft for this section (reject re-run path) or append.
    drafts: list[dict[str, Any]] = [d for d in prior_sections_state if d.get("section") != section]
    drafts.append(
        {
            "section": section,
            "artifact": result.section.model_dump(mode="json"),
            "cited_keys": list(result.cited_keys),
        }
    )

    _log.info("node_draft_section_done", section=section)
    return {
        **state,
        "phase": Phase.DRAFTING,
        "drafts": drafts,
        "current_section": section,
        # Surface the Scribe's LLM usage for this section so the section gate
        # handler can write it to audit_log and the per-project cost cap
        # (NFR-5) can see Phase-4 spend, not just Phase-2. Overwritten on each
        # section draft; the gate handler persists it before the next draft.
        # Includes draft_ms (per-section latency) for Phase-4 telemetry.
        "drafting_usage": usage_payload,
        # Reset on each draft so the gate sees a fresh decision.
        "section_approval": None,
        # Clear feedback so a later approve doesn't accidentally re-inject it.
        "last_feedback": None,
        "awaiting_approval": False,
    }


async def node_await_section_approval(state: GraphState) -> GraphState:
    """HITL gate for Phase 4 — runs once per section.

    Mirrors ``node_await_synthesis_approval``. On override the
    human-edited section artifact replaces ``drafts[-1].artifact`` so the
    final manuscript concatenates the human version, and the section is
    advanced (the override implies approval).
    """
    section = state.get("current_section")
    _log.info(
        "gate_section_approval_waiting",
        project_id=str(state.get("project_id")),
        section=section,
    )

    approval = interrupt(
        {
            "phase": Phase.DRAFTING,
            "section": section,
            "message": f"Review and approve the drafted {section}.",
        }
    )

    if approval != "approve":
        if approval != "reject":
            _log.warning(
                "gate_section_unknown_resume",
                project_id=str(state.get("project_id")),
                value=str(approval)[:64],
            )
        # Reject — leave current_section + sections_remaining unchanged so
        # node_draft_section re-runs the same section. last_feedback is
        # injected by reject_workflow's Command.update.
        _log.info("gate_section_rejected", section=section)
        return {**state, "awaiting_approval": False, "section_approval": "reject"}

    # Approve path — pop current_section out of remaining into done.
    drafts = list(state.get("drafts", []) or [])
    done = list(state.get("sections_done", []) or [])
    remaining = [s for s in state.get("sections_remaining", []) if s != section]
    if section and section not in done:
        done.append(section)

    # Override: replace drafts[-1].artifact with the human-edited version.
    override = state.get("last_override")
    if isinstance(override, dict) and override.get("kind") == "section":
        for d in drafts:
            if d.get("section") == section:
                d["artifact"] = override
                break
        else:
            drafts.append({"section": section, "artifact": override, "cited_keys": []})
        _log.info("gate_section_override", section=section)

    new_state: GraphState = {
        **state,
        "awaiting_approval": False,
        "section_approval": "approve",
        "drafts": drafts,
        "sections_done": done,
        "sections_remaining": remaining,
        # Clear current_section so the next draft node picks the next remaining.
        "current_section": None,
        "last_override": None,
        "last_feedback": None,
    }
    _log.info("gate_section_approved", section=section, remaining=len(remaining))
    return new_state


# Pattern used by both the Scribe and the assembler to find `[@key]` markers
# in the rendered prose. Single source of truth — kept here so the assembler
# stays independent of agents/scribe.py internals.
_CITATION_MARKER_RE = re.compile(r"\[@([A-Za-z0-9_\-:.]+)\]")


def _build_references_section(
    approved_pool: list[dict[str, Any]],
    body_markdown: str,
) -> str:
    """Build the manuscript's ``## References`` section.

    Scans the body markdown for ``[@key]`` markers, resolves each key to a
    paper in ``approved_pool``, and formats a numbered reference list with
    title, authors, year, and a resolvable URL (pdf_url > DOI > external_id
    URL fallback). Closes BRD §10 mitigation row 1 ("post-generation
    validator rejects unknown citation keys") AND row 2 ("audit trail /
    AI-disclosure appendix").

    A citation key that appears in the body but NOT in the approved pool is
    surfaced under a separate "Citations not in pool" subsection. This is
    defence-in-depth — by the time node_assemble runs, the Scribe's
    per-section validator should have already retried or flagged offenders
    on the section gate, but the assembler refuses to silently drop them.
    """
    pool_by_key: dict[str, dict[str, Any]] = {}
    for raw in approved_pool:
        key = str(raw.get("citation_key") or "").strip()
        if key:
            pool_by_key[key] = raw

    # Preserve first-occurrence order so the References numbering matches the
    # reading order of the manuscript.
    cited_in_order: list[str] = []
    seen: set[str] = set()
    for match in _CITATION_MARKER_RE.finditer(body_markdown):
        key = match.group(1)
        if key not in seen:
            seen.add(key)
            cited_in_order.append(key)

    if not cited_in_order:
        return ""

    resolved: list[str] = []
    unresolved: list[str] = []
    for idx, key in enumerate(cited_in_order, start=1):
        paper = pool_by_key.get(key)
        if paper is None:
            unresolved.append(key)
            continue
        resolved.append(_format_reference_entry(idx, key, paper))

    parts = ["## References", ""]
    if resolved:
        parts.extend(resolved)
    if unresolved:
        parts.extend(
            [
                "",
                "### Citations not in approved pool",
                "",
                "The following citation keys appeared in the manuscript body but "
                "could not be resolved against the approved paper pool. These "
                "are likely Scribe hallucinations that escaped the citation "
                "validator; treat the corresponding claims with skepticism:",
                "",
            ]
        )
        for key in unresolved:
            parts.append(f"- `[@{key}]` — unresolved")
    return "\n".join(parts) + "\n"


def _format_reference_entry(idx: int, key: str, paper: dict[str, Any]) -> str:
    """Format one numbered reference line as plain markdown.

    Falls back gracefully on missing metadata — never crashes on a half-
    populated paper row.
    """
    title = str(paper.get("title") or "(no title)").strip()
    authors_raw = paper.get("authors") or []
    if isinstance(authors_raw, list):
        authors = [str(a).strip() for a in authors_raw if str(a).strip()]
    else:
        authors = []
    if not authors:
        author_str = "Unknown authors"
    elif len(authors) <= 3:
        author_str = ", ".join(authors)
    else:
        author_str = f"{', '.join(authors[:3])}, et al."
    year = paper.get("year")
    year_str = str(year) if year else "n.d."

    # Resolve a URL — prefer the explicit pdf_url, then fall back to a DOI
    # lookup, then the source-specific URL. The fallback mirrors the
    # frontend's paperSourceUrl() so links are consistent across UI and PDF.
    pdf_url = str(paper.get("pdf_url") or "").strip()
    external_id = str(paper.get("external_id") or "").strip()
    source = str(paper.get("source") or "").strip()
    url: str | None = None
    if pdf_url:
        url = pdf_url
    elif external_id.startswith(("10.",)):  # DOI shape
        url = f"https://doi.org/{external_id}"
    elif source == "arxiv" and external_id:
        url = f"https://arxiv.org/abs/{external_id}"
    elif source == "semantic_scholar" and external_id:
        url = f"https://www.semanticscholar.org/paper/{external_id}"
    elif source == "europe_pmc" and external_id.startswith("PMC"):
        url = f"https://europepmc.org/article/PMC/{external_id[3:]}"

    url_part = f" — {url}" if url else ""
    return f"{idx}. **[@{key}]** {author_str} ({year_str}). *{title}*.{url_part}"


def _build_disclosure_block(telemetry: dict[str, Any], approved_count: int) -> str:
    """Render the AI-disclosure preamble that goes at the top of the manuscript.

    Per BRD §10 mitigation: "Built-in audit trail and exportable AI-
    disclosure appendix; clear UI labelling of AI-generated vs human-edited
    spans." This block makes the AI provenance unambiguous and frontloads
    the most damaging stat (small pool size) so readers cannot miss it.
    """
    sources = telemetry.get("sources_queried") or []
    sources_str = ", ".join(str(s) for s in sources) if sources else "open academic APIs"
    candidate_count = telemetry.get("candidate_count")
    candidate_part = f" returning {candidate_count} candidate papers" if candidate_count else ""
    return (
        "## AI-generated review — disclosure\n"
        "\n"
        "> This manuscript was produced by an automated multi-agent pipeline "
        "(ResearchFlow AI) with a human-in-the-loop at every phase boundary. "
        f"A Librarian agent queried {sources_str}{candidate_part}; a human "
        f"user approved **{approved_count} paper{'s' if approved_count != 1 else ''}** "
        "as the working pool; a Critic agent produced the structured "
        "comparison; a Scribe agent (LLM) drafted each section, gated by a "
        "human approval step. **The reviewed pool is intentionally small.** "
        "All claims should be read as observations within this AI-curated "
        "sample, not as conclusions about the broader field.\n"
    )


async def node_assemble(state: GraphState) -> GraphState:
    """Concatenate all approved section drafts into a single manuscript artifact.

    Drafts are re-ordered into the canonical seven-section sequence regardless
    of approval order, so a user who rejected the methodology last (re-drafted
    it as the final iteration) still gets the manuscript in proper order.

    The assembler also:
      - Prepends an **AI-disclosure block** that names the pipeline and the
        pool size (BRD §10 mitigation — academic-integrity disclosure).
      - Appends a **References** section resolving every ``[@key]`` marker
        used in the body to a real paper in the approved pool (BRD FR-1.5
        citation manager + §10 hallucination mitigation).
    """
    project_id = state.get("project_id")
    _log.info("node_assemble_start", project_id=str(project_id))

    drafts = state.get("drafts", []) or []
    drafts_by_section: dict[str, dict[str, Any]] = {
        d["section"]: d for d in drafts if isinstance(d, dict) and "section" in d
    }

    ordered_parts: list[str] = []
    for section in _CANONICAL_SECTIONS:
        d = drafts_by_section.get(section)
        if d is None:
            # Defence-in-depth: a missing section is silently absent — the gate
            # invariant means this should never happen at runtime.
            _log.warning("node_assemble_missing_section", section=section)
            continue
        artifact = d.get("artifact") or {}
        body = str(artifact.get("content") or "").strip()
        if body:
            ordered_parts.append(body)

    body_markdown = "\n\n".join(ordered_parts)

    approved_pool: list[dict[str, Any]] = list(state.get("approved_pool") or [])
    telemetry: dict[str, Any] = dict(state.get("workflow_telemetry") or {})
    approved_count = (
        int(telemetry.get("approved_count") or 0)
        if telemetry.get("approved_count") is not None
        else len(approved_pool)
    )

    disclosure_block = _build_disclosure_block(telemetry, approved_count)
    references_block = _build_references_section(approved_pool, body_markdown)

    title_page = (
        f"# Manuscript\n\n"
        f"Generated by ResearchFlow AI · {datetime.now(tz=UTC).date().isoformat()}\n\n"
        f"---\n\n"
    )

    manuscript_md = title_page + disclosure_block + "\n" + body_markdown + "\n"
    if references_block:
        manuscript_md += "\n" + references_block

    pid = project_id if project_id is not None else uuid4()
    manuscript_artifact = Artifact(
        id=uuid4(),
        project_id=pid,
        kind="manuscript",
        label="manuscript",
        content=manuscript_md,
        mime_type="text/markdown",
        produced_by="scribe",
        created_at=datetime.now(tz=UTC),
    )

    _log.info("node_assemble_done", project_id=str(pid), bytes=len(manuscript_md))
    return {
        **state,
        "phase": Phase.DONE,
        "manuscript": manuscript_artifact.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# Edge routing helpers
# ---------------------------------------------------------------------------


def _route_after_pool(state: GraphState) -> str:
    """After the approval gate, decide where to go.

    If the gate was rejected (pool_approval == "reject"),
    loop back to discover. Otherwise advance to synthesize.
    """
    # If rejected the gate node returns awaiting_approval=False without
    # advancing the phase — we detect that and re-run discover.
    if state.get("pool_approval") == "reject":
        return NODE_DISCOVER
    return NODE_SYNTHESIZE


def _route_after_synthesis(state: GraphState) -> str:
    """After the synthesis gate, decide where to go.

    If rejected, loop back to `synthesize` (re-runs the Critic with feedback).
    Otherwise advance to drafting. Phase 3 (analyze) is out of MVP scope.
    """
    if state.get("synthesis_approval") == "reject":
        return NODE_SYNTHESIZE
    return NODE_DRAFT


def _route_after_section(state: GraphState) -> str:
    """After the per-section gate, decide where to go next.

    - reject  → re-draft the *same* section (current_section unchanged).
    - approve + sections_remaining non-empty → draft next section.
    - approve + sections_remaining empty → assemble final manuscript.
    """
    if state.get("section_approval") == "reject":
        return NODE_DRAFT
    if state.get("sections_remaining"):
        return NODE_DRAFT
    return NODE_ASSEMBLE


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(checkpointer: Any) -> Any:
    """Construct and compile the LangGraph state machine.

    `checkpointer` must be an AsyncPostgresSaver (or MemorySaver for tests).
    The caller (lifespan hook or workflow service) is responsible for
    initialising and closing the checkpointer connection pool.

    Phase 1 interrupts before `NODE_SYNTHESIZE` — the graph pauses there
    after `node_await_pool_approval` hands control to LangGraph's interrupt
    mechanism.
    """
    g = StateGraph(GraphState)

    # Register nodes
    g.add_node(NODE_DISCOVER, node_discover)
    g.add_node(NODE_AWAIT_POOL, node_await_pool_approval)
    g.add_node(NODE_SYNTHESIZE, node_synthesize)
    g.add_node(NODE_AWAIT_SYNTHESIS, node_await_synthesis_approval)
    g.add_node(NODE_DRAFT, node_draft_section)
    g.add_node(NODE_AWAIT_SECTION, node_await_section_approval)
    g.add_node(NODE_ASSEMBLE, node_assemble)

    # Entry point
    g.set_entry_point(NODE_DISCOVER)

    # discover → await_pool_approval (always)
    g.add_edge(NODE_DISCOVER, NODE_AWAIT_POOL)

    # await_pool_approval → synthesize or back to discover
    g.add_conditional_edges(
        NODE_AWAIT_POOL,
        _route_after_pool,
        {NODE_SYNTHESIZE: NODE_SYNTHESIZE, NODE_DISCOVER: NODE_DISCOVER},
    )

    # synthesize → await_synthesis_approval (always)
    g.add_edge(NODE_SYNTHESIZE, NODE_AWAIT_SYNTHESIS)

    # await_synthesis_approval → draft_section or back to synthesize
    g.add_conditional_edges(
        NODE_AWAIT_SYNTHESIS,
        _route_after_synthesis,
        {NODE_SYNTHESIZE: NODE_SYNTHESIZE, NODE_DRAFT: NODE_DRAFT},
    )

    # draft_section → await_section_approval (gate)
    g.add_edge(NODE_DRAFT, NODE_AWAIT_SECTION)

    # await_section_approval → next section or assemble (or re-draft on reject)
    g.add_conditional_edges(
        NODE_AWAIT_SECTION,
        _route_after_section,
        {NODE_DRAFT: NODE_DRAFT, NODE_ASSEMBLE: NODE_ASSEMBLE},
    )

    g.add_edge(NODE_ASSEMBLE, END)

    # Compile — using modern checkpointer. Node itself calls interrupt() internally,
    # avoiding redundant external double-interrupts.
    compiled = g.compile(
        checkpointer=checkpointer,
    )
    return compiled


# ---------------------------------------------------------------------------
# Checkpointer factory (used by the lifespan hook and tests)
# ---------------------------------------------------------------------------


async def create_postgres_checkpointer(
    database_url: str,
) -> Any:
    """Create a pool-backed AsyncPostgresSaver and run setup().

    Uses psycopg3's AsyncConnectionPool so connections stay alive across
    background asyncio tasks. The pool is owned by the caller — call
    `.conn.close()` on shutdown.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pg_url = database_url.replace("postgresql+asyncpg://", "postgresql://")
    _pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = cast(
        "AsyncConnectionPool[AsyncConnection[dict[str, Any]]]",
        AsyncConnectionPool(
            conninfo=pg_url,
            max_size=5,
            open=False,
            kwargs={"row_factory": dict_row},
        ),
    )
    await _pool.open()
    saver = AsyncPostgresSaver(conn=_pool)
    await saver.setup()
    return saver
