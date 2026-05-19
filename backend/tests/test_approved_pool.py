"""Tests for B2: approve_workflow must build an approved-pool snapshot.

SPEC.md §5.2 (approve → synthesize) + §6.2 (Critic input: approved_papers).
After approval, state["approved_pool"] must contain the DB-approved papers,
and an audit entry with action="phase_1.approved_pool" must be written.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db import AuditLogRow, Base, PaperRow, ProjectRow, UserRow, WorkflowRunRow
from app.models.schemas import Phase

TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000002")
TEST_PROJECT_ID = uuid4()
TEST_RUN_ID = uuid4()


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session with pre-seeded project + run + papers."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(tz=UTC)
    async with factory() as session:
        session.add(
            UserRow(
                id=TEST_USER_ID,
                firebase_uid="uid-b2",
                email="b2@example.com",
                created_at=now,
            )
        )
        session.add(
            ProjectRow(
                id=TEST_PROJECT_ID,
                owner_id=TEST_USER_ID,
                title="B2 Project",
                seed_query="approved pool",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            WorkflowRunRow(
                id=TEST_RUN_ID,
                project_id=TEST_PROJECT_ID,
                phase=Phase.DISCOVERY.value,
                state="awaiting_approval",
                checkpoint_id=str(uuid4()),
                started_at=now,
                awaiting_since=now,
                last_event_at=now,
            )
        )
        # Two approved papers, one unapproved.
        session.add(
            PaperRow(
                id=uuid4(),
                project_id=TEST_PROJECT_ID,
                source="arxiv",
                external_id="arxiv:001",
                title="Paper One",
                authors=["Alice"],
                year=2024,
                citation_key="alice2024",
                approved=True,
                added_at=now,
            )
        )
        session.add(
            PaperRow(
                id=uuid4(),
                project_id=TEST_PROJECT_ID,
                source="semantic_scholar",
                external_id="ss:002",
                title="Paper Two",
                authors=["Bob"],
                year=2023,
                citation_key="bob2023",
                approved=True,
                added_at=now,
            )
        )
        session.add(
            PaperRow(
                id=uuid4(),
                project_id=TEST_PROJECT_ID,
                source="arxiv",
                external_id="arxiv:003",
                title="Paper Three (unapproved)",
                authors=["Carol"],
                year=2022,
                citation_key="carol2022",
                approved=False,
                added_at=now,
            )
        )
        await session.commit()

    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_approve_workflow_writes_approved_pool_to_state(db_session: AsyncSession) -> None:
    """approve_workflow must pass approved papers into state["approved_pool"] via Command."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from app.graph.state import GraphState
    from app.graph.workflow import build_graph
    from app.models.schemas import Phase
    from app.services.workflow import approve_workflow

    # Build a real graph so ainvoke works.
    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(TEST_RUN_ID)}}

    # Pre-seed a checkpoint at the gate so ainvoke(Command(resume=...)) works.
    with patch("app.graph.workflow.Librarian") as mock_lib:
        mock_lib.return_value.run = AsyncMock(
            return_value=MagicMock(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        initial: GraphState = {
            "project_id": TEST_PROJECT_ID,
            "workflow_run_id": TEST_RUN_ID,
            "seed_query": "test",
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
        await graph.ainvoke(initial, config)

    captured_command: list[Command] = []
    original_ainvoke = graph.ainvoke

    async def spy_ainvoke(cmd, cfg):
        captured_command.append(cmd)
        return await original_ainvoke(cmd, cfg)

    with patch("app.services.workflow._emit", new_callable=AsyncMock):
        with patch("app.services.workflow._update_run_state", new_callable=AsyncMock):
            with patch.object(graph, "ainvoke", side_effect=spy_ainvoke):
                with patch("app.services.workflow.get_compiled_graph", return_value=graph):
                    await approve_workflow(db_session, TEST_PROJECT_ID, TEST_RUN_ID, TEST_USER_ID)

    # The Command passed to ainvoke must include approved papers in update.
    assert len(captured_command) == 1
    cmd = captured_command[0]
    assert cmd.resume == "approve"
    approved_pool = cmd.update.get("approved_pool", [])  # type: ignore[union-attr]
    assert len(approved_pool) == 2
    keys = {p["citation_key"] for p in approved_pool}
    assert keys == {"alice2024", "bob2023"}


@pytest.mark.asyncio
async def test_approve_workflow_writes_audit_pool_entry(db_session: AsyncSession) -> None:
    """approve_workflow must write an audit entry with action='phase_1.approved_pool'."""
    from langgraph.checkpoint.memory import MemorySaver

    from app.graph.state import GraphState
    from app.graph.workflow import build_graph
    from app.models.schemas import Phase
    from app.services.workflow import approve_workflow

    graph = build_graph(MemorySaver())
    config = {"configurable": {"thread_id": str(TEST_RUN_ID)}}

    with patch("app.graph.workflow.Librarian") as mock_lib:
        mock_lib.return_value.run = AsyncMock(
            return_value=MagicMock(candidates=[], expanded_queries=[], arxiv_categories=[])
        )
        initial: GraphState = {
            "project_id": TEST_PROJECT_ID,
            "workflow_run_id": TEST_RUN_ID,
            "seed_query": "test",
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
        await graph.ainvoke(initial, config)

    with patch("app.services.workflow._emit", new_callable=AsyncMock):
        with patch("app.services.workflow._update_run_state", new_callable=AsyncMock):
            with patch("app.services.workflow.get_compiled_graph", return_value=graph):
                await approve_workflow(db_session, TEST_PROJECT_ID, TEST_RUN_ID, TEST_USER_ID)

    await db_session.flush()

    audit_rows = (
        (
            await db_session.execute(
                select(AuditLogRow).where(
                    AuditLogRow.project_id == TEST_PROJECT_ID,
                    AuditLogRow.action == "phase_1.approved_pool",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audit_rows) == 1
    assert "citation_keys" in audit_rows[0].payload
    assert set(audit_rows[0].payload["citation_keys"]) == {"alice2024", "bob2023"}
