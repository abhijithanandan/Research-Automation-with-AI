"""LangGraph state schema for the research workflow."""

from __future__ import annotations

from typing import TypedDict
from uuid import UUID

from app.models.schemas import Artifact, Paper, Phase


class GraphState(TypedDict, total=False):
    """The canonical state passed through every node.

    `phase` and `awaiting_approval` are the two fields the HITL gate logic
    inspects to decide whether to interrupt.
    """

    project_id: UUID
    workflow_run_id: UUID
    phase: Phase

    # Discovery inputs / outputs
    seed_query: str
    expanded_queries: list[str]

    # Phase 1
    candidates: list[Paper]
    approved_pool: list[Paper]

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
