"""Tests for B4: /workflow/override must write ArtifactRow + audit entry.

SPEC.md §7.3 + docs/workflow/state-machine.md §Reject vs Override.
override_workflow must:
  1. Insert an ArtifactRow with produced_by="human".
  2. Write an audit entry with action="user.override".
  3. Pass the artifact into the graph state as last_override.
  4. Advance the gate (resume="approve").
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

from app.models.db import ArtifactRow, AuditLogRow, Base, ProjectRow, UserRow, WorkflowRunRow
from app.models.schemas import Phase

TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000003")
TEST_PROJECT_ID = uuid4()
TEST_RUN_ID = uuid4()


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    """In-memory SQLite session with a pre-seeded awaiting_approval run."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime.now(tz=UTC)
    async with factory() as session:
        session.add(
            UserRow(
                id=TEST_USER_ID,
                firebase_uid="uid-b4",
                email="b4@example.com",
                created_at=now,
            )
        )
        session.add(
            ProjectRow(
                id=TEST_PROJECT_ID,
                owner_id=TEST_USER_ID,
                title="B4 Project",
                seed_query="override test",
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
        await session.commit()

    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_override_writes_artifact_row(db_session: AsyncSession) -> None:
    """override_workflow must persist an ArtifactRow with produced_by='human'."""
    import asyncio

    from app.services.workflow import override_workflow

    with patch("app.services.workflow._emit", new_callable=AsyncMock):
        with patch("app.services.workflow._update_run_state", new_callable=AsyncMock):
            with patch("app.services.workflow.get_compiled_graph", return_value=MagicMock()):
                with patch("app.services.workflow._resume_graph", new_callable=AsyncMock):
                    await override_workflow(
                        db_session,
                        project_id=TEST_PROJECT_ID,
                        run_id=TEST_RUN_ID,
                        user_id=TEST_USER_ID,
                        artifact_kind="log",
                        label="Manual override",
                        content="Manually curated pool.",
                        mime_type="text/plain",
                    )
                    await asyncio.sleep(0)

    await db_session.flush()

    artifacts = (
        (
            await db_session.execute(
                select(ArtifactRow).where(ArtifactRow.project_id == TEST_PROJECT_ID)
            )
        )
        .scalars()
        .all()
    )
    assert len(artifacts) == 1
    assert artifacts[0].produced_by == "human"
    assert artifacts[0].kind == "log"
    assert artifacts[0].label == "Manual override"
    assert artifacts[0].content == "Manually curated pool."


@pytest.mark.asyncio
async def test_override_writes_audit_entry(db_session: AsyncSession) -> None:
    """override_workflow must write an audit entry with action='user.override'."""
    import asyncio

    from app.services.workflow import override_workflow

    with patch("app.services.workflow._emit", new_callable=AsyncMock):
        with patch("app.services.workflow._update_run_state", new_callable=AsyncMock):
            with patch("app.services.workflow.get_compiled_graph", return_value=MagicMock()):
                with patch("app.services.workflow._resume_graph", new_callable=AsyncMock):
                    await override_workflow(
                        db_session,
                        project_id=TEST_PROJECT_ID,
                        run_id=TEST_RUN_ID,
                        user_id=TEST_USER_ID,
                        artifact_kind="summary",
                        label="Curated summary",
                        content="# Summary\n...",
                        mime_type="text/markdown",
                    )
                    await asyncio.sleep(0)

    await db_session.flush()

    audit = (
        (
            await db_session.execute(
                select(AuditLogRow).where(
                    AuditLogRow.project_id == TEST_PROJECT_ID,
                    AuditLogRow.action == "user.override",
                )
            )
        )
        .scalars()
        .first()
    )
    assert audit is not None
    assert audit.actor == "user"
    assert str(TEST_USER_ID) in str(audit.payload.get("user_id", ""))


@pytest.mark.asyncio
async def test_override_passes_artifact_to_graph(db_session: AsyncSession) -> None:
    """override_workflow must pass last_override into the graph Command update via _resume_graph."""
    import asyncio
    from langgraph.types import Command

    from app.services.workflow import override_workflow

    captured: list[Command] = []

    async def spy_resume(project_id, run_id, graph, config, command, done_state):
        captured.append(command)

    with patch("app.services.workflow._emit", new_callable=AsyncMock):
        with patch("app.services.workflow._update_run_state", new_callable=AsyncMock):
            with patch("app.services.workflow.get_compiled_graph", return_value=MagicMock()):
                with patch("app.services.workflow._resume_graph", side_effect=spy_resume):
                    await override_workflow(
                        db_session,
                        project_id=TEST_PROJECT_ID,
                        run_id=TEST_RUN_ID,
                        user_id=TEST_USER_ID,
                        artifact_kind="matrix",
                        label="Human matrix",
                        content="row1,row2",
                        mime_type="text/csv",
                    )
                    await asyncio.sleep(0)

    assert len(captured) == 1
    cmd = captured[0]
    assert cmd.resume == "approve"
    assert cmd.update is not None
    assert "last_override" in cmd.update
    lo = cmd.update["last_override"]
    assert lo["produced_by"] == "human"
    assert lo["label"] == "Human matrix"


@pytest.mark.asyncio
async def test_override_route_calls_override_workflow(db_session: AsyncSession) -> None:
    """The /override HTTP route must call override_workflow, not approve_workflow."""
    from httpx import ASGITransport, AsyncClient
    from unittest.mock import patch as _patch

    from app.api import deps
    from app.models.schemas import User, WorkflowRun

    app = _make_test_app()

    test_user = User(
        id=TEST_USER_ID,
        email="b4@example.com",
        created_at=datetime.now(tz=UTC),
    )

    async def _get_test_user() -> User:
        return test_user

    async def _get_test_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[deps.get_current_user] = _get_test_user
    app.dependency_overrides[deps.get_db_session] = _get_test_session

    mock_return = WorkflowRun(
        id=TEST_RUN_ID,
        project_id=TEST_PROJECT_ID,
        phase=Phase.DISCOVERY,
        state="approved",
        checkpoint_id="ckpt",
        started_at=datetime.now(tz=UTC),
        last_event_at=datetime.now(tz=UTC),
    )

    with _patch("app.services.workflow.override_workflow", new_callable=AsyncMock) as mock_ov:
        mock_ov.return_value = mock_return

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/projects/{TEST_PROJECT_ID}/workflow/override",
                json={
                    "artifact_kind": "log",
                    "label": "Test override",
                    "content": "content",
                    "mime_type": "text/plain",
                },
                headers={"Authorization": "Bearer test-token"},
            )

    assert resp.status_code == 200
    mock_ov.assert_called_once()
    # Verify the run_id was routed correctly (not confused with project_id).
    call_kwargs = mock_ov.call_args.kwargs
    assert call_kwargs["run_id"] == TEST_RUN_ID
    assert call_kwargs["project_id"] == TEST_PROJECT_ID


def _make_test_app():
    from unittest.mock import AsyncMock, patch

    with patch("app.graph.workflow.create_postgres_checkpointer", new_callable=AsyncMock) as cp:
        cp.return_value.setup = AsyncMock()
        cp.return_value.aclose = AsyncMock()
        with patch("app.services.workflow.init_graph", new_callable=AsyncMock):
            from app.main import create_app

            return create_app()
