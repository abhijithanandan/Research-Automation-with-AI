"""Tests for B3: _run_graph race condition between DB state and graph interrupt.

SPEC.md §7.1 + docs/workflow/state-machine.md §Implementation notes.
GraphInterrupt must be caught explicitly; DB state must be set to
awaiting_approval on interrupt, and to error only on a real exception.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver

from app.agents.librarian import LibrarianOutput
from app.graph.workflow import build_graph
from app.models.schemas import Phase

TEST_PROJECT_ID = uuid4()


@pytest.mark.asyncio
async def test_run_graph_sets_awaiting_approval_on_interrupt() -> None:
    """When the graph interrupts at the gate, DB state must become awaiting_approval."""
    run_id = uuid4()
    graph = build_graph(MemorySaver())

    mock_output = LibrarianOutput(candidates=[], expanded_queries=[], arxiv_categories=[])

    update_states: list[str] = []

    async def capture_state(session, rid, state):
        update_states.append(state)

    with patch("app.graph.workflow.Librarian") as mock_lib:
        mock_lib.return_value.run = AsyncMock(return_value=mock_output)

        with patch("app.services.workflow.get_compiled_graph", return_value=graph):
            with patch("app.services.workflow._emit", new_callable=AsyncMock):
                with patch("app.services.workflow._update_run_state", side_effect=capture_state):
                    with patch("app.services.workflow._persist_candidates", new_callable=AsyncMock):
                        # get_session is a late import — patch at source module.
                        with patch("app.db.session.get_session") as mock_get_session:
                            mock_session = AsyncMock()
                            mock_ctx = MagicMock()
                            mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                            mock_ctx.__aexit__ = AsyncMock(return_value=False)
                            mock_get_session.return_value = mock_ctx

                            from app.services.workflow import _run_graph

                            await _run_graph(run_id, TEST_PROJECT_ID, "test")

    # Must have been set to awaiting_approval, NOT error.
    assert "awaiting_approval" in update_states
    assert "error" not in update_states


@pytest.mark.asyncio
async def test_run_graph_sets_error_on_real_exception() -> None:
    """A genuine exception (not GraphInterrupt) must set DB state to error."""
    run_id = uuid4()

    update_states: list[str] = []

    async def capture_state(session, rid, state):
        update_states.append(state)

    broken_graph = MagicMock()
    broken_graph.ainvoke = AsyncMock(side_effect=RuntimeError("LLM exploded"))

    with patch("app.services.workflow.get_compiled_graph", return_value=broken_graph):
        with patch("app.services.workflow._emit", new_callable=AsyncMock):
            with patch("app.services.workflow._update_run_state", side_effect=capture_state):
                with patch("app.db.session.get_session") as mock_get_session:
                    mock_session = AsyncMock()
                    mock_ctx = MagicMock()
                    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_ctx.__aexit__ = AsyncMock(return_value=False)
                    mock_get_session.return_value = mock_ctx

                    from app.services.workflow import _run_graph

                    await _run_graph(run_id, TEST_PROJECT_ID, "test")

    assert "error" in update_states
    assert "awaiting_approval" not in update_states


@pytest.mark.asyncio
async def test_run_graph_does_not_emit_approval_on_error() -> None:
    """approval.required must NOT be emitted when the graph raises a real error."""
    run_id = uuid4()
    emitted_types: list[str] = []

    async def capture_emit(project_id, event):
        emitted_types.append(event.get("type", ""))

    broken_graph = MagicMock()
    broken_graph.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("app.services.workflow.get_compiled_graph", return_value=broken_graph):
        with patch("app.services.workflow._emit", side_effect=capture_emit):
            with patch("app.services.workflow._update_run_state", new_callable=AsyncMock):
                with patch("app.db.session.get_session") as mock_get_session:
                    mock_session = AsyncMock()
                    mock_ctx = MagicMock()
                    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_ctx.__aexit__ = AsyncMock(return_value=False)
                    mock_get_session.return_value = mock_ctx

                    from app.services.workflow import _run_graph

                    await _run_graph(run_id, TEST_PROJECT_ID, "test")

    assert "approval.required" not in emitted_types
    assert "agent.error" in emitted_types
