"""Tests for B1: Librarian candidates are persisted as PaperRows after discovery.

SPEC.md §2.3 (papers table) + §3.4 (PATCH approve contract).
The graph node itself has no DB — persistence is done in _run_graph after ainvoke.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.agents.librarian import LibrarianOutput
from app.graph.workflow import build_graph
from app.models.db import Base, PaperRow, ProjectRow, UserRow
from app.models.schemas import Paper

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
TEST_PROJECT_ID = uuid4()


def _mock_paper(citation_key: str = "author2024", project_id: UUID | None = None) -> Paper:
    return Paper(
        id=uuid4(),
        project_id=project_id or TEST_PROJECT_ID,
        source="arxiv",  # type: ignore[arg-type]
        external_id=f"arxiv:{citation_key}",
        title=f"Mock Paper {citation_key}",
        authors=["Author, A"],
        year=2024,
        abstract="An abstract.",
        citation_key=citation_key,
        approved=False,
        added_at=datetime.now(tz=UTC),
    )


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session with all tables created."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed a user and project row (needed for FK constraints on Postgres;
    # SQLite won't enforce FKs but we seed anyway for realism).
    async with factory() as session:
        now = datetime.now(tz=UTC)
        session.add(
            UserRow(
                id=TEST_USER_ID,
                firebase_uid="test-firebase-uid",
                email="test@example.com",
                created_at=now,
            )
        )
        session.add(
            ProjectRow(
                id=TEST_PROJECT_ID,
                owner_id=TEST_USER_ID,
                title="Test Project",
                seed_query="AI agents",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------------
# B1 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_candidates_persisted_after_discover(db_session: AsyncSession) -> None:
    """After _run_graph completes the discover phase, PaperRows must exist in the DB."""
    from app.services.workflow import _persist_candidates

    papers = [_mock_paper("lecun1989a"), _mock_paper("bengio2003b")]
    run_id = uuid4()

    await _persist_candidates(db_session, TEST_PROJECT_ID, run_id, papers)
    await db_session.flush()

    rows = (
        (await db_session.execute(select(PaperRow).where(PaperRow.project_id == TEST_PROJECT_ID)))
        .scalars()
        .all()
    )

    assert len(rows) == 2
    keys = {r.citation_key for r in rows}
    assert keys == {"lecun1989a", "bengio2003b"}
    assert all(not r.approved for r in rows)


@pytest.mark.asyncio
async def test_persist_candidates_idempotent(db_session: AsyncSession) -> None:
    """Re-running persist with the same citation keys must not create duplicates."""
    from app.services.workflow import _persist_candidates

    papers = [_mock_paper("smith2020")]
    run_id = uuid4()

    await _persist_candidates(db_session, TEST_PROJECT_ID, run_id, papers)
    await db_session.flush()
    # Call again with same data.
    await _persist_candidates(db_session, TEST_PROJECT_ID, run_id, papers)
    await db_session.flush()

    rows = (
        (await db_session.execute(select(PaperRow).where(PaperRow.project_id == TEST_PROJECT_ID)))
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_persist_candidates_approved_always_false(db_session: AsyncSession) -> None:
    """Persisted rows must always have approved=False regardless of input."""
    from app.services.workflow import _persist_candidates

    # Construct a paper with approved=True (invariant violation input).
    p = _mock_paper("risky2024")
    p_dict = p.model_dump()
    p_dict["approved"] = True
    bad_paper = Paper(**p_dict)

    run_id = uuid4()
    await _persist_candidates(db_session, TEST_PROJECT_ID, run_id, [bad_paper])
    await db_session.flush()

    row = (
        (
            await db_session.execute(
                select(PaperRow).where(
                    PaperRow.project_id == TEST_PROJECT_ID,
                    PaperRow.citation_key == "risky2024",
                )
            )
        )
        .scalars()
        .first()
    )
    assert row is not None
    assert row.approved is False


@pytest.mark.asyncio
async def test_run_graph_persists_papers() -> None:
    """Integration: _run_graph must call _persist_candidates with the librarian output."""
    papers = [_mock_paper("integrate2024", project_id=TEST_PROJECT_ID)]
    mock_output = LibrarianOutput(candidates=papers, expanded_queries=[], arxiv_categories=[])

    run_id = uuid4()
    graph = build_graph(MemorySaver())

    with patch("app.graph.workflow.Librarian") as mock_lib:
        mock_lib.return_value.run = AsyncMock(return_value=mock_output)

        with patch("app.services.workflow._persist_candidates", new_callable=AsyncMock) as mock_persist:
            with patch("app.services.workflow.get_compiled_graph", return_value=graph):
                # get_session is a late import inside _run_graph — patch at the source module.
                with patch("app.db.session.get_session") as mock_get_session:
                    mock_session = AsyncMock(spec=AsyncSession)
                    mock_ctx = MagicMock()
                    mock_ctx.__aenter__ = AsyncMock(return_value=mock_session)
                    mock_ctx.__aexit__ = AsyncMock(return_value=False)
                    mock_get_session.return_value = mock_ctx

                    with patch("app.services.workflow._emit", new_callable=AsyncMock):
                        with patch("app.services.workflow._update_run_state", new_callable=AsyncMock):
                            from app.services.workflow import _run_graph

                            await _run_graph(run_id, TEST_PROJECT_ID, "test query")

    # _persist_candidates must have been called with project_id, run_id, and the papers list.
    mock_persist.assert_called_once()
    call_args = mock_persist.call_args
    assert call_args.args[1] == TEST_PROJECT_ID
    assert call_args.args[2] == run_id
    # The graph node serialises Paper objects to dicts in state["candidates"];
    # _run_graph reconstructs them via Paper(**d). Verify at least one paper was passed.
    assert len(call_args.args[3]) >= 1
    assert call_args.args[3][0].citation_key == "integrate2024"
