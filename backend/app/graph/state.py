"""LangGraph state schema for the research workflow."""

from __future__ import annotations

from typing import Any, TypedDict
from uuid import UUID

from app.models.schemas import Artifact, Phase


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

    # Phase 2
    matrix: Artifact | None
    summary: Artifact | None

    # Phase 4
    sections_done: list[str]
    sections_remaining: list[str]
    drafts: list[Artifact]

    # Control
    awaiting_approval: bool
    last_feedback: str | None
    last_override: Artifact | None
    pool_approval: str | None
