"""Workflow run-state contract tests (audit P0).

Locks the invariant that NO path can persist an out-of-contract run state —
the bug class behind the orphan-cleanup "failed" defect. Layers tested:

  1. The constant ↔ Literal are in sync (single source of truth holds).
  2. The service chokepoint (_update_run_state) rejects invalid literals.
  3. The DB CHECK constraint rejects invalid literals (defense in depth).
  4. Startup orphan-cleanup moves "running" → "error" (contract-valid), and the
     literal it writes is in VALID_RUN_STATES.
"""

from __future__ import annotations

import typing
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db import Base, ProjectRow, UserRow, WorkflowRunRow
from app.models.schemas import VALID_RUN_STATES, WorkflowRun

# ---------------------------------------------------------------------------
# 1. Constant ↔ Literal sync
# ---------------------------------------------------------------------------


def test_valid_run_states_matches_workflowrun_literal() -> None:
    """VALID_RUN_STATES must equal the WorkflowRun.state Literal — they are the
    single source of truth and a drift between them is the exact failure mode
    that let "failed" slip through."""
    literal_args = set(typing.get_args(WorkflowRun.model_fields["state"].annotation))
    assert literal_args == set(VALID_RUN_STATES), (
        f"VALID_RUN_STATES {sorted(VALID_RUN_STATES)} is out of sync with the "
        f"WorkflowRun.state Literal {sorted(literal_args)}"
    )


def test_failed_is_not_a_valid_run_state() -> None:
    """Explicit guard against the regression: 'failed' is NOT contract-valid."""
    assert "failed" not in VALID_RUN_STATES
    assert "error" in VALID_RUN_STATES


# ---------------------------------------------------------------------------
# 2. Service chokepoint rejects invalid literals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_state", ["failed", "FAILED", "done", "cancelled", "", "running "])
async def test_update_run_state_rejects_invalid_literal(bad_state: str) -> None:
    """_update_run_state raises ValueError before touching the DB for any state
    not in VALID_RUN_STATES."""
    from app.services.workflow import _update_run_state

    session = MagicMock()
    session.execute = AsyncMock()
    with pytest.raises(ValueError, match="invalid workflow run state"):
        await _update_run_state(session, uuid4(), bad_state)
    session.execute.assert_not_called()  # rejected before any SQL


@pytest.mark.asyncio
@pytest.mark.parametrize("good_state", sorted(VALID_RUN_STATES))
async def test_update_run_state_accepts_every_valid_literal(good_state: str) -> None:
    """Every contract-valid state passes the guard and issues the UPDATE."""
    from app.services.workflow import _update_run_state

    session = MagicMock()
    session.execute = AsyncMock()
    await _update_run_state(session, uuid4(), good_state)
    session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. DB CHECK constraint rejects invalid literals
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def seeded_db() -> AsyncIterator[tuple[AsyncSession, object]]:
    """In-memory SQLite with FK + CHECK enforcement, seeded with a user+project."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        # SQLite needs PRAGMA to enforce FKs; CHECK is enforced by default.
        await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        await conn.run_sync(Base.metadata.create_all)
    now = datetime.now(tz=UTC)
    project_id, owner_id = uuid4(), uuid4()
    async with factory() as s:
        s.add(UserRow(id=owner_id, firebase_uid="u", email="u@x.com", created_at=now))
        await s.flush()  # user must exist before the project's owner_id FK resolves
        s.add(
            ProjectRow(
                id=project_id,
                owner_id=owner_id,
                title="t",
                seed_query="q",
                created_at=now,
                updated_at=now,
            )
        )
        await s.flush()
        yield s, project_id
    await engine.dispose()


@pytest.mark.asyncio
async def test_db_check_constraint_rejects_invalid_state(
    seeded_db: tuple[AsyncSession, object],
) -> None:
    """Inserting a WorkflowRunRow with an out-of-contract state must raise at
    the DB layer (the ck_workflow_runs_state_valid CHECK), even if some future
    code path bypasses _update_run_state."""
    s, project_id = seeded_db
    now = datetime.now(tz=UTC)
    s.add(
        WorkflowRunRow(
            id=uuid4(),
            project_id=project_id,
            phase="discovery",
            state="failed",  # NOT in the contract
            checkpoint_id=str(uuid4()),
            started_at=now,
            last_event_at=now,
        )
    )
    with pytest.raises(IntegrityError):
        await s.flush()


@pytest.mark.asyncio
async def test_db_check_constraint_accepts_valid_state(
    seeded_db: tuple[AsyncSession, object],
) -> None:
    """A contract-valid state inserts cleanly under the CHECK constraint."""
    s, project_id = seeded_db
    now = datetime.now(tz=UTC)
    s.add(
        WorkflowRunRow(
            id=uuid4(),
            project_id=project_id,
            phase="discovery",
            state="error",
            checkpoint_id=str(uuid4()),
            started_at=now,
            last_event_at=now,
        )
    )
    await s.flush()  # must not raise


# ---------------------------------------------------------------------------
# 4. Startup orphan-cleanup invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orphan_cleanup_moves_running_to_error(
    seeded_db: tuple[AsyncSession, object],
) -> None:
    """The orphan-cleanup UPDATE (running → error) must produce a contract-valid
    state. Replicates the exact statement from main._cleanup_orphaned_runs and
    asserts the result is 'error' (NOT 'failed') and passes the CHECK."""
    s, project_id = seeded_db
    now = datetime.now(tz=UTC)
    run_id = uuid4()
    s.add(
        WorkflowRunRow(
            id=run_id,
            project_id=project_id,
            phase="discovery",
            state="running",
            checkpoint_id=str(uuid4()),
            started_at=now,
            last_event_at=now,
        )
    )
    await s.flush()

    # Exact cleanup statement from main.py.
    await s.execute(
        update(WorkflowRunRow).where(WorkflowRunRow.state == "running").values(state="error")
    )
    await s.flush()

    row = await s.get(WorkflowRunRow, run_id)
    assert row is not None
    assert row.state == "error"
    assert row.state in VALID_RUN_STATES


def test_main_orphan_cleanup_literal_is_error_not_failed() -> None:
    """Source-level guard: main.py must write 'error' for orphans, never the
    non-contract 'failed' that the audit P0 flagged."""
    from pathlib import Path

    main_src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text(encoding="utf-8")
    assert 'values(state="failed")' not in main_src, "orphan cleanup must NOT write 'failed'"
    assert 'orphan_state = "error"' in main_src


@pytest.mark.asyncio
async def test_orphan_cleanup_only_touches_running_runs(
    seeded_db: tuple[AsyncSession, object],
) -> None:
    """Cleanup must only re-state orphaned 'running' rows — never touch
    awaiting_approval/approved runs (those are not orphans)."""
    s, project_id = seeded_db
    now = datetime.now(tz=UTC)
    # An approved run that must be left alone.
    approved_id = uuid4()
    s.add(
        WorkflowRunRow(
            id=approved_id,
            project_id=project_id,
            phase="synthesis",
            state="approved",
            checkpoint_id=str(uuid4()),
            started_at=now,
            last_event_at=now,
        )
    )
    await s.flush()

    await s.execute(
        update(WorkflowRunRow).where(WorkflowRunRow.state == "running").values(state="error")
    )
    await s.flush()

    row = await s.get(WorkflowRunRow, approved_id)
    assert row is not None
    assert row.state == "approved", "non-running runs must be untouched by cleanup"
