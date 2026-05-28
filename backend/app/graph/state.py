"""LangGraph state schema for the research workflow."""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from app.models.schemas import Phase


class GraphState(TypedDict, total=False):
    """The canonical state passed through every node.

    `phase` and `awaiting_approval` are the two fields the HITL gate logic
    inspects to decide whether to interrupt.

    Note: `candidates` and `approved_pool` store **dicts** (not Paper objects)
    because LangGraph's MemorySaver serialises state via msgpack, which cannot
    handle Pydantic models directly.  Nodes that need Paper objects should
    rehydrate with ``Paper(**d)`` on read.
    """

    project_id: UUID
    workflow_run_id: UUID
    phase: Phase

    # Discovery inputs / outputs
    seed_query: str
    expanded_queries: list[str]

    # Phase 1 — stored as dicts for checkpoint serialisation
    candidates: list[dict[str, Any]]
    approved_pool: list[dict[str, Any]]

    # Phase 2 — stored as dicts for checkpoint serialisation (like candidates).
    matrix: dict[str, Any] | None
    summary: dict[str, Any] | None
    synthesis_approval: str | None
    # Token/cost rollup for the Critic run — written to audit_log (BRD FR-3.3).
    synthesis_usage: dict[str, Any] | None

    # Phase 4 — Scribe / drafting
    sections_done: list[str]
    sections_remaining: list[str]
    drafts: list[dict[str, Any]]
    # Which section the Scribe is drafting / the user is reviewing right now.
    # None outside Phase 4; populated by node_draft_section before pausing.
    current_section: str | None
    # Set by approve_workflow / reject_workflow on the resume Command.
    section_approval: str | None
    # Populated by node_assemble after the last section approves.
    manuscript: dict[str, Any] | None
    # Token/cost rollup for the most recent Scribe section draft — written to
    # audit_log by the section gate handler so the cost cap (NFR-5) sees
    # Phase-4 spend. Overwritten per section.
    drafting_usage: dict[str, Any] | None

    # Workflow telemetry — the *real* description of what the agentic pipeline
    # did during this run. The Scribe consumes this when writing the
    # manuscript's Methodology section so that the prose reflects the actual
    # agentic workflow (BRD §10 risk mitigation: no fabricated human-style
    # literature-review process). Shape — kept loose because each node owns
    # its own slice:
    #   {
    #     "sources_queried":   list[str]   # e.g. ["semantic_scholar","arxiv","crossref","core","europe_pmc"]
    #     "sources_with_hits": list[str]   # subset that actually returned >=1 paper
    #     "expanded_queries":  list[str]   # the seed-query expansions the Librarian used
    #     "candidate_count":   int         # how many papers were initially fetched
    #     "approved_count":    int         # how many the user kept in the approved pool
    #     "deduplicated_count": int        # candidate_count - duplicates
    #     "rag_available":     bool        # whether the Critic was able to use vector RAG
    #     "discovery_started_at": str ISO  # when node_discover began
    #     "discovery_finished_at": str ISO
    #     "synthesis_started_at": str ISO
    #     "synthesis_finished_at": str ISO
    #   }
    workflow_telemetry: dict[str, Any]

    # Control
    awaiting_approval: bool
    last_feedback: str | None
    last_override: dict[str, Any] | None  # stored as dict for checkpoint serialisation
    pool_approval: str | None
