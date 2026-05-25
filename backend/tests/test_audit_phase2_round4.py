"""Regression tests for round-4 audit findings.

Maps:
  - MED-2  → flush_for_background_dispatch is used at every site that needs
             commit-before-dispatch (replaces ad-hoc session.commit() calls).
  - MED-4  → broad except Exception now (a) re-raises CancelledError and
             (b) attaches a structured error_code to the emitted agent.error
             event so incidents are diagnosable from logs/WS stream.
  - LOW-MED (paper lock) → audit-marker layer locks the pool independently
             of run.phase / run.state, so future state-machine bugs cannot
             unlock the pool.
  - LOW (ws rate limit) → handshake rate limit closes excess connections
             from the same peer with code 4429.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

# ---------------------------------------------------------------------------
# MED-2 — flush_for_background_dispatch helper exists and is wired everywhere
# session.commit() used to be called mid-handler.
# ---------------------------------------------------------------------------


def test_flush_helper_exists_and_is_documented() -> None:
    """The helper must exist, be importable, and carry a docstring explaining
    when to use it. The docstring is part of the contract — future readers
    must see why a commit happens mid-handler."""
    from app.db.session import flush_for_background_dispatch

    assert callable(flush_for_background_dispatch)
    doc = flush_for_background_dispatch.__doc__ or ""
    assert "background" in doc.lower()
    assert "create_task" in doc or "background" in doc.lower()


def test_workflow_service_has_no_bare_session_commit() -> None:
    """Round-4 MED-2: every site that needs to flush before spawning a
    background task must go through flush_for_background_dispatch, not call
    session.commit() directly. This test guards against regressions where
    someone adds a new dispatch site and forgets the named helper."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "workflow.py"
    text = src.read_text(encoding="utf-8")
    # Strip out comments; we don't want to fail because a comment mentions
    # "session.commit()" while explaining the policy.
    code_lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code = "\n".join(code_lines)
    # The only acceptable commit call is via the helper.
    assert "session.commit()" not in code, (
        "Found a raw session.commit() in workflow.py — use "
        "flush_for_background_dispatch(session) instead so the intent is "
        "explicit at the call site (audit round-4 MED-2)."
    )


# ---------------------------------------------------------------------------
# MED-4 — broad except Exception now emits a structured error_code and
# does NOT swallow asyncio.CancelledError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_graph_emits_structured_error_code_on_failure() -> None:
    """When the graph raises a typed exception, the agent.error event must
    include error_code so dashboards/logs can group by exception class —
    not just the freeform string."""
    from app.services import workflow as wf

    project_id = uuid4()
    run_id = uuid4()

    captured: list[dict] = []

    async def fake_emit(_pid, evt):  # type: ignore[no-untyped-def]
        captured.append(evt)

    class BoomError(RuntimeError):
        """Marker class so we can assert the exact name in the event."""

    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(side_effect=BoomError("nope"))

    with (
        patch.object(wf, "_emit", new=fake_emit),
        patch.object(wf, "get_compiled_graph", return_value=fake_graph),
        patch.object(wf, "_update_run_state", new=AsyncMock()),
    ):
        await wf._run_graph(run_id, project_id, "seed")

    error_events = [e for e in captured if e.get("type") == "agent.error"]
    assert error_events, "expected an agent.error event to be emitted"
    assert error_events[0]["error_code"] == "BoomError"
    assert "error" in error_events[0]


@pytest.mark.asyncio
async def test_run_graph_propagates_cancelled_error() -> None:
    """asyncio.CancelledError must propagate (lifespan shutdown). If the
    broad-except handler swallows it, graceful shutdown becomes impossible
    and the workflow background task lingers as a zombie."""
    from app.services import workflow as wf

    project_id = uuid4()
    run_id = uuid4()

    fake_graph = MagicMock()
    fake_graph.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())

    with (
        patch.object(wf, "_emit", new=AsyncMock()),
        patch.object(wf, "get_compiled_graph", return_value=fake_graph),
        patch.object(wf, "_update_run_state", new=AsyncMock()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await wf._run_graph(run_id, project_id, "seed")


# ---------------------------------------------------------------------------
# LOW-MED — audit-marker layer locks the pool even if run.phase/state
# are not in their normal post-approval shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paper_lock_fires_on_audit_marker_alone() -> None:
    """If a phase_1.approved_pool audit row exists, the pool must be locked
    even when run.phase=='discovery' and run.state=='running' — the audit
    record is the source-of-truth, run.* are derived state."""
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.api.routes.papers import _assert_phase_not_locked
    from app.models.db import AuditLogRow, Base, ProjectRow, UserRow, WorkflowRunRow

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
        # Run is still in the "running / discovery" state — heuristic layer
        # would NOT lock here. Only the audit marker should fire.
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
        setup.add(
            AuditLogRow(
                id=uuid4(),
                project_id=project_id,
                workflow_run_id=run_id,
                actor="user",
                action="phase_1.approved_pool",
                payload={"approved_count": 5},
                created_at=now,
            )
        )
        await setup.commit()

    async def _yield_session() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s

    from fastapi import HTTPException

    async for s in _yield_session():
        with pytest.raises(HTTPException) as exc_info:
            await _assert_phase_not_locked(s, project_id)  # type: ignore[arg-type]
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["code"] == "phase_locked"  # type: ignore[index]

    await engine.dispose()


# ---------------------------------------------------------------------------
# LOW — WS per-IP handshake rate limit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_rate_limiter_allows_then_blocks() -> None:
    """First N connections from one IP succeed; the (N+1)th is throttled.
    Verifies the sliding-window logic, not the WS plumbing."""
    from app.api.routes.websocket import (
        _WS_RATE_MAX_PER_WINDOW,
        _check_handshake_rate_limit,
        _ws_connect_timestamps,
    )

    test_ip = f"203.0.113.{uuid4().int % 250}"  # unique per test, RFC5737
    _ws_connect_timestamps.pop(test_ip, None)

    for _ in range(_WS_RATE_MAX_PER_WINDOW):
        assert await _check_handshake_rate_limit(test_ip) is True
    # (N+1)th must be rejected.
    assert await _check_handshake_rate_limit(test_ip) is False


@pytest.mark.asyncio
async def test_ws_rate_limiter_is_per_ip() -> None:
    """A flooded IP must not block other IPs."""
    from app.api.routes.websocket import (
        _WS_RATE_MAX_PER_WINDOW,
        _check_handshake_rate_limit,
        _ws_connect_timestamps,
    )

    ip_a = f"198.51.100.{uuid4().int % 250}"
    ip_b = f"198.51.100.{(uuid4().int % 250) + 1}"
    _ws_connect_timestamps.pop(ip_a, None)
    _ws_connect_timestamps.pop(ip_b, None)

    # Exhaust ip_a.
    for _ in range(_WS_RATE_MAX_PER_WINDOW):
        assert await _check_handshake_rate_limit(ip_a) is True
    assert await _check_handshake_rate_limit(ip_a) is False

    # ip_b is untouched.
    assert await _check_handshake_rate_limit(ip_b) is True
