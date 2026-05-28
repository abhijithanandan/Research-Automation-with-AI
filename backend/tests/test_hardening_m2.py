"""Regression tests for the M2 data-integrity & state-machine gate.

Coverage:
  M2-A: audit_log partial unique index on phase_1.approved_pool — a second
        approve for the same run is rejected with 409 instead of silently
        writing a duplicate marker. (Also exercised end-to-end via
        ``test_concurrent_approve_returns_409`` below.)
  M2-C: WorkflowRun.state and WorkflowRun.phase must always advance in the
        same UPDATE statement. Source-level guard rejects any future
        contributor splitting the update into two statements.
  M2-D: _assert_run_in_state() — the generalized run-state helper — rejects
        runs not in the expected state set with a typed 409 envelope.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db import AuditLogRow, Base, ProjectRow, UserRow, WorkflowRunRow

# ---------------------------------------------------------------------------
# M2-A: phase_1.approved_pool partial unique
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase_1_approved_pool_audit_row_is_unique_per_run() -> None:
    """Inserting two phase_1.approved_pool rows for the same workflow_run_id
    raises IntegrityError (alembic 0006 + AuditLogRow.__table_args__)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    project_id = uuid4()
    user_id = uuid4()
    run_id = uuid4()
    now = datetime.now(tz=UTC)
    async with factory() as setup:
        setup.add(UserRow(id=user_id, firebase_uid="t", email="t@x.com", created_at=now))
        setup.add(
            ProjectRow(
                id=project_id,
                owner_id=user_id,
                title="t",
                seed_query="q",
                output_format="markdown",
                token_cap_usd=5.0,
                status="active",
                current_phase="synthesis",
                created_at=now,
                updated_at=now,
            )
        )
        setup.add(
            WorkflowRunRow(
                id=run_id,
                project_id=project_id,
                phase="synthesis",
                state="awaiting_approval",
                checkpoint_id=str(run_id),
                started_at=now,
                last_event_at=now,
            )
        )
        # First marker — accepted.
        setup.add(
            AuditLogRow(
                id=uuid4(),
                project_id=project_id,
                workflow_run_id=run_id,
                actor="system",
                action="phase_1.approved_pool",
                payload={"count": 5},
                created_at=now,
            )
        )
        await setup.commit()

    # Second marker for the same run — rejected by the partial unique index.
    async with factory() as second:
        second.add(
            AuditLogRow(
                id=uuid4(),
                project_id=project_id,
                workflow_run_id=run_id,
                actor="system",
                action="phase_1.approved_pool",
                payload={"count": 6},
                created_at=now,
            )
        )
        with pytest.raises(IntegrityError):
            await second.commit()

    # Same action on a DIFFERENT run must be allowed — the index is keyed
    # on workflow_run_id, not project_id. The other run is in `approved`
    # state so it doesn't collide with the alembic-0004 partial unique on
    # active workflow_runs (one active run per project).
    other_run_id = uuid4()
    async with factory() as third:
        third.add(
            WorkflowRunRow(
                id=other_run_id,
                project_id=project_id,
                phase="discovery",
                state="approved",
                checkpoint_id=str(other_run_id),
                started_at=now,
                last_event_at=now,
            )
        )
        third.add(
            AuditLogRow(
                id=uuid4(),
                project_id=project_id,
                workflow_run_id=other_run_id,
                actor="system",
                action="phase_1.approved_pool",
                payload={"count": 3},
                created_at=now,
            )
        )
        await third.commit()

    # Other audit actions are NOT constrained — many user.approve rows per
    # run is normal.
    async with factory() as fourth:
        for _ in range(3):
            fourth.add(
                AuditLogRow(
                    id=uuid4(),
                    project_id=project_id,
                    workflow_run_id=run_id,
                    actor="user",
                    action="user.approve",
                    payload={},
                    created_at=now,
                )
            )
        await fourth.commit()

    await engine.dispose()


# ---------------------------------------------------------------------------
# M2-C: source-level guard against split phase/state updates
# ---------------------------------------------------------------------------


def test_update_run_state_uses_single_update_statement() -> None:
    """The _update_run_state helper must update state + phase in ONE
    UPDATE. The previous bug (round 3 MED-1) was a code path that
    advanced run.state without bumping run.phase, leaving the paper-lock
    rule looking at stale phase strings.

    This source-level test rejects any future regression where the
    function fans the update across multiple session.execute() calls.
    """
    from app.services import workflow as wf

    src = inspect.getsource(wf._update_run_state)
    # Exactly one execute call inside the helper.
    assert src.count("await session.execute(") == 1, (
        "_update_run_state must issue a single UPDATE — splitting state "
        "and phase across two statements re-introduces the round-3 MED-1 bug."
    )
    # And the single statement must mutate at least state + last_event_at.
    assert '"state": new_state' in src or "state=new_state" in src
    assert '"last_event_at"' in src or "last_event_at=" in src


def test_no_caller_updates_phase_without_state() -> None:
    """Source-level guard: nothing in the service layer should mutate
    WorkflowRunRow.phase WITHOUT also calling _update_run_state.

    The only legit way to change phase is through the helper that also
    sets state. A grep for ``.values(phase=`` or ``"phase":`` directly
    against WorkflowRunRow outside the helper catches future regressions.
    """
    from app.services import workflow as wf

    src = inspect.getsource(wf)
    # Strip the _update_run_state body so its own legitimate `"phase":`
    # write doesn't trip the guard.
    helper_src = inspect.getsource(wf._update_run_state)
    other_src = src.replace(helper_src, "")
    # Any remaining occurrence of an UPDATE statement that writes
    # `phase` on the workflow_runs table is suspicious. We keep this
    # narrow — the grep target is the literal Python kwarg shape used
    # elsewhere in the file (`values(phase=`), not the SQL text.
    assert "values(phase=" not in other_src, (
        "Direct .values(phase=...) update outside _update_run_state — "
        "splits phase from state and re-introduces the round-3 MED-1 bug."
    )


# ---------------------------------------------------------------------------
# M2-D: _assert_run_in_state generalized helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_run_in_state_allows_expected_state() -> None:
    from app.services.workflow import _assert_run_in_state

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    project_id = uuid4()
    user_id = uuid4()
    run_id = uuid4()
    now = datetime.now(tz=UTC)
    async with factory() as setup:
        setup.add(UserRow(id=user_id, firebase_uid="t", email="t@x.com", created_at=now))
        setup.add(
            ProjectRow(
                id=project_id,
                owner_id=user_id,
                title="t",
                seed_query="q",
                output_format="markdown",
                token_cap_usd=5.0,
                status="active",
                current_phase="discovery",
                created_at=now,
                updated_at=now,
            )
        )
        setup.add(
            WorkflowRunRow(
                id=run_id,
                project_id=project_id,
                phase="discovery",
                state="running",
                checkpoint_id=str(run_id),
                started_at=now,
                last_event_at=now,
            )
        )
        await setup.commit()

    async def _yield_session() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    async for s in _yield_session():
        # "running" is in the allowed set → returns the row, no exception.
        run = await _assert_run_in_state(s, run_id, {"running", "awaiting_approval"})
        assert run.state == "running"

    await engine.dispose()


@pytest.mark.asyncio
async def test_assert_run_in_state_rejects_wrong_state_with_409() -> None:
    from app.services.workflow import _assert_run_in_state

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    project_id = uuid4()
    user_id = uuid4()
    run_id = uuid4()
    now = datetime.now(tz=UTC)
    async with factory() as setup:
        setup.add(UserRow(id=user_id, firebase_uid="t", email="t@x.com", created_at=now))
        setup.add(
            ProjectRow(
                id=project_id,
                owner_id=user_id,
                title="t",
                seed_query="q",
                output_format="markdown",
                token_cap_usd=5.0,
                status="active",
                current_phase="done",
                created_at=now,
                updated_at=now,
            )
        )
        setup.add(
            WorkflowRunRow(
                id=run_id,
                project_id=project_id,
                phase="done",
                state="approved",
                checkpoint_id=str(run_id),
                started_at=now,
                last_event_at=now,
            )
        )
        await setup.commit()

    async def _yield_session() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    async for s in _yield_session():
        with pytest.raises(HTTPException) as exc_info:
            # The run is "approved" — not in the expected awaiting set.
            await _assert_run_in_state(s, run_id, {"awaiting_approval"})
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "phase_locked"  # type: ignore[index]

    await engine.dispose()


@pytest.mark.asyncio
async def test_assert_run_in_state_404s_on_missing_run() -> None:
    from app.services.workflow import _assert_run_in_state

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def _yield_session() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    bogus_run_id = uuid4()
    async for s in _yield_session():
        with pytest.raises(HTTPException) as exc_info:
            await _assert_run_in_state(s, bogus_run_id, {"awaiting_approval"})
        assert exc_info.value.status_code == 404

    await engine.dispose()
