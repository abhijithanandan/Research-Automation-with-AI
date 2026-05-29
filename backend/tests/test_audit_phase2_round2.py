"""Regression tests for the second-pass audit findings (HIGH-2 + MED-1/2/3).

Each test corresponds to a finding in the external review. See the
"Pre-Push Audit Report Addendum" for the full mapping.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db import Base, UserRow
from app.models.schemas import Phase

# ---------------------------------------------------------------------------
# Identity model — `users.id` is a DB-authoritative surrogate key (uuid4),
# resolved by looking up `firebase_uid`. NOT derived from the UID. This is the
# replacement for the removed `_stable_uuid_from_uid` (reviewer MED finding:
# custom hash-based identity derivation is an audit-portability risk).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _resolve_user_id(db: AsyncSession, uid: str, email: str) -> UUID:
    """Mirror deps.get_current_user's identity resolution: look up by
    firebase_uid; insert with a DB-default uuid4 surrogate on first sight."""
    existing = await db.scalar(select(UserRow).where(UserRow.firebase_uid == uid))
    if existing is not None:
        return existing.id
    row = UserRow(firebase_uid=uid, email=email, created_at=datetime.now(tz=UTC))
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row.id


@pytest.mark.asyncio
async def test_new_user_gets_uuid4_surrogate_key(db_session: AsyncSession) -> None:
    """A first-seen user is assigned a standard uuid4 (version 4) by the DB
    column default — not a value computed from the firebase_uid."""
    uid = await _resolve_user_id(db_session, "firebase-uid-abc-123", "abc@example.com")
    assert uid.version == 4


@pytest.mark.asyncio
async def test_identity_stable_across_requests_via_lookup(db_session: AsyncSession) -> None:
    """Same firebase_uid resolves to the same internal id on every request —
    now via DB lookup on the natural key, not via re-derivation."""
    a = await _resolve_user_id(db_session, "alice@example.com", "alice@example.com")
    b = await _resolve_user_id(db_session, "alice@example.com", "alice@example.com")
    assert a == b


@pytest.mark.asyncio
async def test_identity_differs_per_uid(db_session: AsyncSession) -> None:
    """Different Firebase UIDs map to distinct internal ids (no aliasing)."""
    a = await _resolve_user_id(db_session, "alice@example.com", "alice@example.com")
    b = await _resolve_user_id(db_session, "bob@example.com", "bob@example.com")
    assert a != b


@pytest.mark.asyncio
async def test_identity_not_derived_from_uid(db_session: AsyncSession) -> None:
    """The internal id must NOT be a deterministic function of the UID — that
    is the whole point of the surrogate-key migration. Two fresh DBs would
    assign different ids to the same uid (proving DB-authoritative, not
    derived)."""
    from uuid import NAMESPACE_DNS, uuid5

    uid = "example.com"
    assigned = await _resolve_user_id(db_session, uid, "x@example.com")
    # It is not the old UUIDv5 derivation, and not the DNS-namespace v5 either.
    assert assigned != uuid5(NAMESPACE_DNS, uid)
    assert assigned.version == 4


# ---------------------------------------------------------------------------
# MED-1 — _update_run_state now accepts a new_phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_run_state_persists_new_phase() -> None:
    """The phase parameter must land in the SQL UPDATE values dict."""
    from unittest.mock import AsyncMock, MagicMock

    from app.services.workflow import _update_run_state

    session = MagicMock()
    session.execute = AsyncMock()

    await _update_run_state(session, uuid4(), "approved", new_phase=Phase.SYNTHESIS)

    # The execute call's first positional arg is an Update statement. We
    # introspect the compiled values to confirm `phase` was included.
    update_stmt = session.execute.call_args.args[0]
    bound = update_stmt.compile().params
    assert bound["phase"] == "synthesis"
    assert bound["state"] == "approved"


@pytest.mark.asyncio
async def test_update_run_state_omits_phase_when_not_supplied() -> None:
    """Backward-compat: callers that don't care about phase don't bump it."""
    from unittest.mock import AsyncMock, MagicMock

    from app.services.workflow import _update_run_state

    session = MagicMock()
    session.execute = AsyncMock()

    await _update_run_state(session, uuid4(), "running")

    update_stmt = session.execute.call_args.args[0]
    bound = update_stmt.compile().params
    # phase must NOT be in the values dict — leave the existing row value alone.
    assert "phase" not in bound


def test_next_phase_after_gate_mapping() -> None:
    """The phase-advancement lookup table covers every gate transition."""
    from app.services.workflow import _NEXT_PHASE_AFTER_GATE

    assert _NEXT_PHASE_AFTER_GATE["discovery"] == Phase.SYNTHESIS
    assert _NEXT_PHASE_AFTER_GATE["synthesis"] == Phase.DRAFTING
    assert _NEXT_PHASE_AFTER_GATE["drafting"] == Phase.DONE
    # No mapping for "done" — terminal state.
    assert "done" not in _NEXT_PHASE_AFTER_GATE


# ---------------------------------------------------------------------------
# MED-3 — paper-lock rule
# ---------------------------------------------------------------------------


class _FakeRun:
    """Minimal stand-in for WorkflowRunRow used in lock-rule tests."""

    def __init__(self, state: str, phase: str) -> None:
        self.state = state
        self.phase = phase


def _evaluate_lock(state: str, phase: str) -> bool:
    """Mirror of _assert_phase_not_locked's inner decision rule.

    Keeping this in lock-step with papers.py is part of the regression
    contract — if the source rule changes, this test must change too.
    """
    return phase != "discovery" or state == "approved" or state == "error"


@pytest.mark.parametrize(
    ("state", "phase", "should_lock"),
    [
        # Pre-approval states — never locked.
        ("running", "discovery", False),
        ("awaiting_approval", "discovery", False),
        ("rejected", "discovery", False),
        # Approval transition — locked (state is the legacy backstop).
        ("approved", "discovery", True),
        # Post-MED-1 state machine — phase advances, locked.
        ("approved", "synthesis", True),
        ("running", "synthesis", True),
        ("awaiting_approval", "synthesis", True),
        ("approved", "drafting", True),
        ("approved", "done", True),
        # Error states — locked regardless of phase.
        ("error", "discovery", True),
        ("error", "synthesis", True),
    ],
)
def test_paper_lock_decision_table(state: str, phase: str, should_lock: bool) -> None:
    assert _evaluate_lock(state, phase) is should_lock


# ---------------------------------------------------------------------------
# HIGH-2 — WebSocket auth log redaction is purely structural; the only way
# to assert it is to invoke the handler with a known exception and confirm
# the resulting log record does NOT carry the exception's str(). That needs
# a full Starlette WebSocket harness which is out of scope here; the unit
# guarantee is the simple swap of error=str(exc) → error_type=type(exc).__name__
# enforced by code review.
# ---------------------------------------------------------------------------


def test_ws_log_uses_error_type_not_message() -> None:
    """Source-level guard: the routes/websocket.py auth handler must not
    write str(exc) into its log — only the exception class name.

    Note: M1-D renamed the structured event ids to a dotted namespace
    (``ws.auth.recv_error`` etc.) so the canonical event marker is
    different from the round-2 string. The invariant we still care about
    is that ``error_type=type(exc).__name__`` is used and bare
    ``str(exc)`` is NOT used in the WS auth path.
    """
    import inspect

    from app.api.routes import websocket as ws_mod

    src = inspect.getsource(ws_mod)
    # M1-D-era event name (dotted namespace) — the file MUST emit a
    # structured recv-error log line, whatever it's called.
    assert "ws.auth.recv_error" in src or "ws_auth_recv_error" in src
    assert "error_type=type(exc).__name__" in src
    # And the bad pattern must not be present anywhere in that file.
    assert "error=str(exc)" not in src


# ---------------------------------------------------------------------------
# Misc sanity check — Phase enum mapping aligns with DB column values.
# ---------------------------------------------------------------------------


def test_phase_enum_values_match_db_strings() -> None:
    """Catch typos like Phase.SYNTHESIS.value == "synthsis"."""
    assert Phase.DISCOVERY.value == "discovery"
    assert Phase.SYNTHESIS.value == "synthesis"
    assert Phase.DRAFTING.value == "drafting"
    assert Phase.DONE.value == "done"


# Smoke imports — surface any circular-import regressions early.
def test_imports_smoke() -> None:
    """Ensure the workflow / deps / papers modules still import cleanly."""
    from app.api.deps import get_current_user
    from app.api.routes import papers, workflow
    from app.services.workflow import _NEXT_PHASE_AFTER_GATE

    assert callable(get_current_user)
    assert callable(papers._assert_phase_not_locked)
    assert callable(workflow.approve)
    assert _NEXT_PHASE_AFTER_GATE  # not empty
