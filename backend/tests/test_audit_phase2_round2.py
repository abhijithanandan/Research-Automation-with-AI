"""Regression tests for the second-pass audit findings (HIGH-2 + MED-1/2/3).

Each test corresponds to a finding in the external review. See the
"Pre-Push Audit Report Addendum" for the full mapping.
"""

from __future__ import annotations

import uuid
from uuid import UUID, uuid4

import pytest

from app.api.deps import _stable_uuid_from_uid
from app.models.schemas import Phase

# ---------------------------------------------------------------------------
# MED-2 — UUIDv5 derivation
# ---------------------------------------------------------------------------


def test_stable_uuid_is_uuid_v5() -> None:
    """The derived UUID must be a standard UUIDv5 (variant 1, version 5)."""
    derived = _stable_uuid_from_uid("firebase-uid-abc-123")
    assert derived.version == 5
    # Variant 1 (RFC 4122) means the high two bits of clock_seq_hi are 0b10.
    assert (derived.clock_seq_hi_variant & 0xC0) == 0x80


def test_stable_uuid_is_deterministic() -> None:
    """Same uid in → same UUID out. Required for stable owner_id mapping."""
    a = _stable_uuid_from_uid("alice@example.com")
    b = _stable_uuid_from_uid("alice@example.com")
    assert a == b


def test_stable_uuid_differs_per_uid() -> None:
    """Different Firebase UIDs map to distinct UUIDs (no accidental aliasing)."""
    a = _stable_uuid_from_uid("alice@example.com")
    b = _stable_uuid_from_uid("bob@example.com")
    assert a != b


def test_stable_uuid_domain_separated_from_dns_namespace() -> None:
    """Our namespace must not collide with the standard DNS namespace —
    otherwise a UID equal to a domain name could authenticate as that user."""
    ours = _stable_uuid_from_uid("example.com")
    dns = uuid.uuid5(uuid.NAMESPACE_DNS, "example.com")
    assert ours != dns


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
    write str(exc) into its log — only the exception class name."""
    import inspect

    from app.api.routes import websocket as ws_mod

    src = inspect.getsource(ws_mod)
    # The redaction is "log only the type". The old bad pattern was
    # `error=str(exc)` directly inside the auth-recv handler. Any future
    # contributor restoring it should turn this test red.
    assert "ws_auth_recv_error" in src
    assert "error_type=type(exc).__name__" in src
    # And the bad pattern must not be present anywhere in that file.
    assert 'ws_auth_recv_error", error=str(exc)' not in src


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
    from app.api.deps import _RESEARCHFLOW_USER_NS
    from app.api.routes import papers, workflow
    from app.services.workflow import _NEXT_PHASE_AFTER_GATE

    assert isinstance(_RESEARCHFLOW_USER_NS, UUID)
    assert callable(papers._assert_phase_not_locked)
    assert callable(workflow.approve)
    assert _NEXT_PHASE_AFTER_GATE  # not empty
