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

    # Phase 4
    sections_done: list[str]
    sections_remaining: list[str]
    drafts: list[dict[str, Any]]

    # Control
    awaiting_approval: bool
    last_feedback: str | None
    last_override: dict[str, Any] | None  # stored as dict for checkpoint serialisation
    pool_approval: str | None
