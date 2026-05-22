"""Tests for the Phase 2 synthesis HITL gate (Phase 2 B-4 / B-6).

SPEC.md §5 + docs/workflow/state-machine.md.
After the Phase 1 pool is approved, the graph runs `node_synthesize` (Critic)
and must then interrupt at `node_await_synthesis_approval`. The service layer
(`_resume_graph`) must detect that the graph is still interrupted and:
  - update workflow_runs.state to "awaiting_approval" (NOT "approved"),
  - emit an `approval.required` event with phase="synthesis".
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.critic import CriticOutput
from app.graph.workflow import (
    NODE_AWAIT_SYNTHESIS,
    NODE_SYNTHESIZE,
    build_graph,
)
from app.models.schemas import Artifact, Phase

TEST_PROJECT_ID = uuid4()


def _critic_output() -> CriticOutput:
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    return CriticOutput(
        matrix=Artifact(
            id=uuid4(),
            project_id=TEST_PROJECT_ID,
            kind="matrix",
            label="literature-matrix",
            content='{"rows": []}',
            mime_type="application/json",
            produced_by="critic",
            created_at=now,
        ),
        summary=Artifact(
            id=uuid4(),
            project_id=TEST_PROJECT_ID,
            kind="summary",
            label="literature-summary",
            content="## Synthesis\n\nNarrative.",
            mime_type="text/markdown",
            produced_by="critic",
            created_at=now,
        ),
    )


def test_graph_registers_synthesis_gate_node() -> None:
    """The compiled graph must contain the synthesis gate node."""
    graph = build_graph(MemorySaver())
    assert NODE_AWAIT_SYNTHESIS in graph.nodes
    assert NODE_SYNTHESIZE in graph.nodes


@pytest.mark.asyncio
async def test_graph_interrupts_at_synthesis_gate_after_pool_approval() -> None:
    """Resuming the pool gate with 'approve' must run synthesize then interrupt again."""
    from langgraph.types import Command

    graph = build_graph(MemorySaver())
    run_id = uuid4()
    config = {"configurable": {"thread_id": str(run_id)}}

    from app.agents.librarian import LibrarianOutput

    librarian_out = LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])

    with patch("app.graph.workflow.Librarian") as mock_lib:
        mock_lib.return_value.run = AsyncMock(return_value=librarian_out)
        with patch("app.graph.workflow.Critic") as mock_critic:
            mock_critic.return_value.run = AsyncMock(return_value=_critic_output())

            initial_state = {
                "project_id": TEST_PROJECT_ID,
                "workflow_run_id": run_id,
                "seed_query": "test",
                "phase": Phase.DISCOVERY,
                "candidates": [],
                "approved_pool": [],
                "awaiting_approval": False,
                "last_feedback": None,
                "last_override": None,
                "expanded_queries": [],
                "sections_done": [],
                "sections_remaining": ["abstract"],
                "drafts": [],
                "matrix": None,
                "summary": None,
                "synthesis_approval": None,
            }
            # First run — interrupts at the pool gate.
            await graph.ainvoke(initial_state, config)
            snapshot = await graph.aget_state(config)
            assert snapshot.next  # interrupted

            # Approve the pool — graph runs synthesize, interrupts at synthesis gate.
            await graph.ainvoke(Command(resume="approve"), config)
            snapshot = await graph.aget_state(config)

    # The graph must still be interrupted, parked at the synthesis gate.
    assert snapshot.next == (NODE_AWAIT_SYNTHESIS,)
    assert snapshot.values.get("phase") == Phase.SYNTHESIS


@pytest.mark.asyncio
async def test_resume_graph_emits_synthesis_approval_required() -> None:
    """_resume_graph must emit approval.required (phase=synthesis) when the graph
    is still interrupted after an approve resume, not state.changed=approved."""
    from langgraph.types import Command

    from app.services.workflow import _resume_graph

    run_id = uuid4()
    emitted: list[dict[str, object]] = []

    async def capture_emit(project_id: object, event: dict[str, object]) -> None:
        emitted.append(event)

    update_states: list[str] = []

    async def capture_state(session: object, rid: object, state: str) -> None:
        update_states.append(state)

    # A graph mock that, after ainvoke, reports it is still interrupted
    # at the synthesis gate.
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value=None)
    snapshot = MagicMock()
    snapshot.next = (NODE_AWAIT_SYNTHESIS,)
    snapshot.values = {
        "phase": Phase.SYNTHESIS,
        "matrix": _critic_output().matrix.model_dump(mode="json"),
        "summary": _critic_output().summary.model_dump(mode="json"),
    }
    graph.aget_state = AsyncMock(return_value=snapshot)

    with patch("app.services.workflow._emit", side_effect=capture_emit):
        with patch("app.services.workflow._update_run_state", side_effect=capture_state):
            with patch("app.services.workflow._persist_artifacts", new_callable=AsyncMock):
                with patch("app.db.session.get_session") as mock_get_session:
                    mock_session = AsyncMock()
                    # `session.add` is synchronous — keep it a plain MagicMock so
                    # _write_audit does not leave an un-awaited coroutine.
                    mock_session.add = MagicMock()
                    mock_ctx = MagicMock()
                    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_ctx.__aexit__ = AsyncMock(return_value=False)
                    mock_get_session.return_value = mock_ctx

                    await _resume_graph(
                        TEST_PROJECT_ID,
                        run_id,
                        graph,
                        {"configurable": {"thread_id": str(run_id)}},
                        Command(resume="approve"),
                        "approved",
                    )

    emitted_types = [e.get("type") for e in emitted]
    assert "approval.required" in emitted_types
    assert "state.changed" not in emitted_types
    # DB state must be awaiting_approval, never approved.
    assert "awaiting_approval" in update_states
    assert "approved" not in update_states
    # The approval.required event carries the synthesis phase.
    approval_evt = next(e for e in emitted if e.get("type") == "approval.required")
    assert approval_evt.get("phase") == Phase.SYNTHESIS.value
