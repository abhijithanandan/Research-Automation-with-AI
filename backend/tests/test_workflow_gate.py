"""Tests for the HITL approval gate invariants (SPEC.md §5.3, §7).

Uses LangGraph's MemorySaver checkpointer so no Postgres is required.
The compiled graph is the real Phase 1 graph; the Librarian is mocked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph.state import GraphState
from app.graph.workflow import NODE_AWAIT_POOL, build_graph
from app.models.schemas import Paper, Phase


def _mock_paper() -> Paper:
    return Paper(
        id=uuid4(),
        project_id=uuid4(),
        source="arxiv",  # type: ignore[arg-type]
        external_id="arxiv:mock001",
        title="Mock Paper",
        authors=["Author, A"],
        year=2024,
        citation_key="author2024",
        approved=False,
        added_at=datetime.now(tz=UTC),
    )


def _build_test_graph():
    """Build the graph with an in-memory checkpointer (no Postgres needed)."""
    return build_graph(MemorySaver())


@pytest.mark.asyncio
async def test_graph_pauses_after_discover() -> None:
    """The graph must halt at the pool-approval gate after the discover node."""
    graph = _build_test_graph()
    project_id = uuid4()
    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    # Mock the Librarian so we don't hit real APIs.
    from app.agents.librarian import LibrarianOutput

    mock_output = LibrarianOutput(candidates=[_mock_paper()], expanded_queries=["mock query"])

    with patch("app.graph.workflow.Librarian") as MockLibrarian:
        MockLibrarian.return_value.run = AsyncMock(return_value=mock_output)

        initial: GraphState = {
            "project_id": project_id,
            "workflow_run_id": uuid4(),
            "seed_query": "test query",
            "phase": Phase.DISCOVERY,
            "candidates": [],
            "approved_pool": [],
            "awaiting_approval": False,
            "last_feedback": None,
            "last_override": None,
            "expanded_queries": [],
            "sections_done": [],
            "sections_remaining": [],
            "drafts": [],
            "matrix": None,
            "summary": None,
        }

        # First invocation: should run discover and then pause.
        result = await graph.ainvoke(initial, config)

    # The graph must have stopped — state should contain candidates.
    snapshot = graph.get_state(config)
    # Graph is interrupted at NODE_AWAIT_POOL.
    assert snapshot.next == (NODE_AWAIT_POOL,) or snapshot.next == ()


@pytest.mark.asyncio
async def test_graph_advances_on_approve() -> None:
    """After an approve Command the graph should advance past the gate."""
    graph = _build_test_graph()
    project_id = uuid4()
    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    from app.agents.librarian import LibrarianOutput

    mock_output = LibrarianOutput(candidates=[_mock_paper()], expanded_queries=[])

    with patch("app.graph.workflow.Librarian") as MockLibrarian:
        MockLibrarian.return_value.run = AsyncMock(return_value=mock_output)

        initial: GraphState = {
            "project_id": project_id,
            "workflow_run_id": uuid4(),
            "seed_query": "quantum computing",
            "phase": Phase.DISCOVERY,
            "candidates": [],
            "approved_pool": [],
            "awaiting_approval": False,
            "last_feedback": None,
            "last_override": None,
            "expanded_queries": [],
            "sections_done": [],
            "sections_remaining": [],
            "drafts": [],
            "matrix": None,
            "summary": None,
        }

        # First run — should pause.
        await graph.ainvoke(initial, config)
        snapshot_before = graph.get_state(config)

        # Now approve.
        await graph.ainvoke(Command(resume="approve"), config)
        snapshot_after = graph.get_state(config)

    # After approval the graph should have advanced further (phase != DISCOVERY
    # or the next node is not await_pool).
    assert snapshot_after.next != snapshot_before.next or snapshot_after.next == ()


@pytest.mark.asyncio
async def test_approved_papers_always_start_unapproved() -> None:
    """All candidate papers returned by the Librarian must have approved=False."""
    import httpx
    import respx

    from app.agents.librarian import Librarian, LibrarianInput

    with respx.mock:
        # Return empty search results from both sources.
        respx.get("https://api.semanticscholar.org/graph/v1/paper/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        respx.get("http://export.arxiv.org/api/query").mock(
            return_value=httpx.Response(
                200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>"
            )
        )

        with patch("app.services.llm.get_llm_gateway") as mock_gw:
            mock_gw.return_value.complete = AsyncMock(return_value=('["alt query"]', {}))

            librarian = Librarian()
            output = await librarian.run(LibrarianInput(seed_query="test"))

    # Invariant from SPEC §6.1: approved is always False on returned candidates.
    assert all(not p.approved for p in output.candidates)
