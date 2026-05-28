"""Regression tests for Phase 4 audit guardrails (B-9).

Defence-in-depth: the paper-pool lock (originally introduced in Phase 2
round-4 LOW-MED) must hold during Phase 4 drafting. Once Phase 1 has been
approved, the pool is the *only* citation source the Scribe is allowed to
use; mutating it underneath an in-flight manuscript draft would break the
citation invariant in docs/agents/scribe.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.routes.papers import _assert_phase_not_locked
from app.models.db import Base, ProjectRow, UserRow, WorkflowRunRow


@pytest.mark.asyncio
async def test_paper_lock_holds_during_drafting() -> None:
    """run.phase='drafting' must lock the paper pool: any PATCH/DELETE on
    /papers/{id} returns 409 phase_locked. The Scribe is mid-manuscript;
    mutating the pool now would corrupt the citation invariant."""
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
                current_phase="drafting",
                created_at=now,
                updated_at=now,
            )
        )
        # Phase 4 in progress — graph parked at a per-section approval gate.
        setup.add(
            WorkflowRunRow(
                id=run_id,
                project_id=project_id,
                phase="drafting",
                state="awaiting_approval",
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
            await _assert_phase_not_locked(s, project_id)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "phase_locked"  # type: ignore[index]

    await engine.dispose()


@pytest.mark.asyncio
async def test_paper_lock_holds_after_drafting_done() -> None:
    """After the manuscript is assembled (phase='done'), the pool stays locked.
    Re-opening the pool post-DONE would invalidate already-generated citations."""
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
                status="completed",
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
            await _assert_phase_not_locked(s, project_id)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 409

    await engine.dispose()
