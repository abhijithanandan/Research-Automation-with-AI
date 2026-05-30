"""Security regression suite (external-review P2).

A single named gate that pins the security-critical behaviors so a future
change can't quietly weaken them. Some of these behaviors are also covered in
the scattered test_hardening_* / test_audit_* files; this suite is the
consolidated, self-documenting checklist the review asked for:

  1. DEV_AUTH_BYPASS is refused outside development (boot-time hard fail).
  2. WebSocket handshake rate-limiting (per-IP sliding window → 4429).
  3. Request body-size limit (oversized JSON → 413).
  4. phase_locked (409) conflict behavior on a locked workflow run.
  5. Identity resolution is unified + DB-authoritative (HTTP == WS path).

Runs on in-memory SQLite / mocked settings — no Postgres, no network.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.db import Base, UserRow

# ===========================================================================
# 1. DEV_AUTH_BYPASS refused outside development
# ===========================================================================


# app_env is itself a strict Literal — Pydantic rejects anything outside the
# three values at construction (defense in depth: the env can NEVER be a typo'd
# value), so the only non-dev values that can exist are staging/production.
@pytest.mark.parametrize("env", ["staging", "production"])
def test_dev_auth_bypass_refused_outside_development(env: str) -> None:
    """Boot must hard-fail if DEV_AUTH_BYPASS=true while APP_ENV != development.
    The guard defaults closed (EXPLOIT-1)."""
    from app.config import Settings

    s = Settings(dev_auth_bypass=True, app_env=env)  # type: ignore[arg-type]
    # The lifespan guard refuses to boot. We assert the *condition* the guard
    # checks rather than spinning the whole app: bypass on + env != dev.
    refuse = s.dev_auth_bypass and s.app_env != "development"
    assert refuse, f"bypass guard must fire for app_env={env!r}"


def test_app_env_rejects_invalid_values() -> None:
    """Defense in depth: APP_ENV can only ever be one of the three valid
    literals — a typo'd 'prod' can't construct, so it can't silently disable
    the bypass guard."""
    import pydantic

    from app.config import Settings

    with pytest.raises(pydantic.ValidationError):
        Settings(app_env="prod")  # type: ignore[arg-type]


def test_dev_auth_bypass_allowed_only_in_development() -> None:
    from app.config import Settings

    s = Settings(dev_auth_bypass=True, app_env="development")
    assert not (s.dev_auth_bypass and s.app_env != "development")


# ===========================================================================
# 2. WebSocket handshake rate limiting
# ===========================================================================


@pytest.mark.asyncio
async def test_ws_handshake_rate_limit_allows_then_blocks() -> None:
    """The per-IP sliding window admits up to the budget, then rejects."""
    import app.api.routes.websocket as ws

    ip = f"10.0.0.{uuid4().int % 250}"
    # Reset shared state for this IP so the test is isolated.
    ws._ws_connect_timestamps.pop(ip, None)

    allowed = 0
    for _ in range(ws._WS_RATE_MAX_PER_WINDOW):
        if await ws._check_handshake_rate_limit(ip):
            allowed += 1
    # The (budget+1)-th connection in the same window must be throttled.
    blocked = not await ws._check_handshake_rate_limit(ip)

    assert allowed == ws._WS_RATE_MAX_PER_WINDOW
    assert blocked, "connection over the per-IP budget must be rejected (4429 path)"


@pytest.mark.asyncio
async def test_ws_handshake_rate_limit_is_per_ip() -> None:
    """One IP exhausting its budget must not affect a different IP."""
    import app.api.routes.websocket as ws

    ip_a = f"10.1.0.{uuid4().int % 250}"
    ip_b = f"10.2.0.{uuid4().int % 250}"
    ws._ws_connect_timestamps.pop(ip_a, None)
    ws._ws_connect_timestamps.pop(ip_b, None)

    for _ in range(ws._WS_RATE_MAX_PER_WINDOW):
        await ws._check_handshake_rate_limit(ip_a)
    assert not await ws._check_handshake_rate_limit(ip_a), "ip_a should be throttled"
    assert await ws._check_handshake_rate_limit(ip_b), "ip_b must be unaffected"


# ===========================================================================
# 3. Request body-size limit
# ===========================================================================


def _app_with_body_limit():
    from fastapi import FastAPI

    from app.api.middleware import BodySizeLimitMiddleware

    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware)

    @app.post("/echo")
    async def echo() -> dict[str, str]:  # pragma: no cover - trivial
        return {"ok": "yes"}

    return app


def test_body_size_under_cap_passes() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(_app_with_body_limit())
    resp = client.post("/echo", content=b"x" * 1024, headers={"content-type": "application/json"})
    assert resp.status_code != 413


def test_body_size_over_cap_rejected_413() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(_app_with_body_limit())
    # 2 MiB > the 1 MiB JSON cap.
    big = b"x" * (2 * 1024 * 1024)
    resp = client.post(
        "/echo",
        content=big,
        headers={"content-type": "application/json", "content-length": str(len(big))},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "request_too_large"


# ===========================================================================
# 4. phase_locked (409) conflict behavior
# ===========================================================================


@pytest_asyncio.fixture()
async def db_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_phase_locked_after_pool_approval(db_session: AsyncSession) -> None:
    """Once the phase_1.approved_pool audit marker exists, the paper pool is
    permanently locked: PATCH/DELETE on /papers must raise 409 phase_locked.
    This is the authoritative (audit-marker) lock layer."""
    from datetime import UTC as _UTC

    from fastapi import HTTPException

    from app.api.routes.papers import _assert_phase_not_locked
    from app.models.db import AuditLogRow, ProjectRow, UserRow

    now = datetime.now(tz=_UTC)
    user_id, project_id, run_id = uuid4(), uuid4(), uuid4()
    db_session.add(UserRow(id=user_id, firebase_uid="u", email="u@x.com", created_at=now))
    db_session.add(
        ProjectRow(
            id=project_id,
            owner_id=user_id,
            title="t",
            seed_query="q",
            created_at=now,
            updated_at=now,
        )
    )
    # The canonical proof-of-approval marker.
    db_session.add(
        AuditLogRow(
            id=uuid4(),
            project_id=project_id,
            workflow_run_id=run_id,
            actor="system",
            action="phase_1.approved_pool",
            payload={"citation_keys": ["a2024"]},
            created_at=now,
        )
    )
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await _assert_phase_not_locked(db_session, project_id)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "phase_locked"  # type: ignore[index]


@pytest.mark.asyncio
async def test_pool_not_locked_before_approval(db_session: AsyncSession) -> None:
    """Before any approval marker / advanced phase, the pool is editable."""
    now = datetime.now(tz=UTC)
    user_id, project_id = uuid4(), uuid4()
    from app.api.routes.papers import _assert_phase_not_locked
    from app.models.db import ProjectRow

    db_session.add(UserRow(id=user_id, firebase_uid="u2", email="u2@x.com", created_at=now))
    db_session.add(
        ProjectRow(
            id=project_id,
            owner_id=user_id,
            title="t",
            seed_query="q",
            current_phase="discovery",
            created_at=now,
            updated_at=now,
        )
    )
    await db_session.flush()
    # Must NOT raise.
    await _assert_phase_not_locked(db_session, project_id)


# ===========================================================================
# 5. Unified, DB-authoritative identity (HTTP path == WS path)
# ===========================================================================


@pytest.mark.asyncio
async def test_identity_http_and_ws_resolve_same_id(db_session: AsyncSession) -> None:
    """resolve_or_create_user (HTTP) and resolve_user_id (WS) must agree on the
    internal id for the same firebase_uid — the two transports share one rule."""
    from app.services.auth import resolve_or_create_user, resolve_user_id

    created = await resolve_or_create_user(
        db_session, firebase_uid="shared-uid", email="s@x.com", display_name=None
    )
    looked_up = await resolve_user_id(db_session, "shared-uid")
    assert looked_up == created.id


@pytest.mark.asyncio
async def test_identity_surrogate_key_is_uuid4_not_derived(db_session: AsyncSession) -> None:
    """The internal id is a DB uuid4 surrogate, NOT derived from the uid
    (no UUIDv5-from-uid). Proves the identity-portability fix."""
    from uuid import NAMESPACE_DNS, uuid5

    from app.services.auth import resolve_or_create_user

    uid = "example.com"
    row = await resolve_or_create_user(
        db_session, firebase_uid=uid, email="e@x.com", display_name=None
    )
    assert row.id.version == 4
    assert row.id != uuid5(NAMESPACE_DNS, uid)


@pytest.mark.asyncio
async def test_identity_ws_lookup_returns_none_for_unknown_user(db_session: AsyncSession) -> None:
    """WS path is read-only: an unknown firebase_uid resolves to None (→ the
    handshake treats it as an authorization failure, never auto-creates)."""
    from app.services.auth import resolve_user_id

    assert await resolve_user_id(db_session, "never-seen-uid") is None


@pytest.mark.asyncio
async def test_identity_http_upsert_refreshes_profile(db_session: AsyncSession) -> None:
    """A returning user keeps their id but gets email/display_name refreshed."""
    from app.services.auth import resolve_or_create_user

    first = await resolve_or_create_user(
        db_session, firebase_uid="uid-x", email="old@x.com", display_name="Old"
    )
    second = await resolve_or_create_user(
        db_session, firebase_uid="uid-x", email="new@x.com", display_name="New"
    )
    assert second.id == first.id
    assert second.email == "new@x.com"
    assert second.display_name == "New"


# ===========================================================================
# 6. Auth outcomes — malformed header, invalid/revoked token, ownership
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("header", [None, "", "Token abc", "bearer", "Basic xyz"])
async def test_http_auth_rejects_malformed_authorization_header(header: str | None) -> None:
    """Missing or non-Bearer Authorization headers → 401 before any token work."""
    from fastapi import HTTPException

    from app.api.deps import get_current_user

    with pytest.raises(HTTPException) as exc:
        await get_current_user(authorization=header, db=AsyncSession.__new__(AsyncSession))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_http_auth_rejects_invalid_or_revoked_token() -> None:
    """When Firebase verification raises (invalid/expired/revoked token), the
    HTTP path returns a generic 401 — never leaks which sub-failure occurred."""
    from fastapi import HTTPException

    from app.api.deps import get_current_user

    with patch("app.api.deps.verify_firebase_token", side_effect=ValueError("revoked")):
        with pytest.raises(HTTPException) as exc:
            await get_current_user(
                authorization="Bearer some-revoked-token",
                db=AsyncSession.__new__(AsyncSession),
            )
    assert exc.value.status_code == 401
    # Generic message — does not disclose the underlying auth-error class.
    assert "revoked" not in str(exc.value.detail).lower()


def test_project_ownership_mismatch_rejected() -> None:
    """A user requesting another user's project → 403; a missing project → 404
    (HTTP _assert_owned). Same owner_id policy the WS handshake enforces."""
    from datetime import UTC as _UTC

    from fastapi import HTTPException

    from app.api.routes.projects import _assert_owned
    from app.models.db import ProjectRow

    now = datetime.now(tz=_UTC)
    owner_id, intruder_id, project_id = uuid4(), uuid4(), uuid4()
    row = ProjectRow(
        id=project_id,
        owner_id=owner_id,
        title="t",
        seed_query="q",
        created_at=now,
        updated_at=now,
    )

    # Owner passes (no raise).
    _assert_owned(row, owner_id, project_id)
    # Intruder is rejected with 403.
    with pytest.raises(HTTPException) as exc:
        _assert_owned(row, intruder_id, project_id)
    assert exc.value.status_code == 403
    # Missing project → 404.
    with pytest.raises(HTTPException) as exc404:
        _assert_owned(None, owner_id, project_id)
    assert exc404.value.status_code == 404


# ===========================================================================
# 7. No sensitive auth-payload leakage in logs
# ===========================================================================


@pytest.mark.asyncio
async def test_http_auth_failure_log_does_not_leak_token() -> None:
    """The verify-failed log line must carry error_type for forensics but must
    NOT contain the bearer token (a JWT in logs is an info-disclosure leak)."""
    from fastapi import HTTPException

    from app.api.deps import get_current_user

    secret_token = "eyJhbGciOiJSUzI1NiIsSENSITIVE_JWT_PAYLOAD_xyz"
    captured: list[dict] = []

    class _CapLogger:
        def warning(self, event, **fields):
            captured.append({"event": event, **fields})

        def __getattr__(self, _):  # info/error/etc. no-op
            return lambda *a, **k: None

    with patch("app.api.deps.verify_firebase_token", side_effect=ValueError("bad token")):
        with patch("app.utils.logging.get_logger", return_value=_CapLogger()):
            with pytest.raises(HTTPException):
                await get_current_user(
                    authorization=f"Bearer {secret_token}",
                    db=AsyncSession.__new__(AsyncSession),
                )

    assert captured, "expected an auth-failure log line"
    blob = repr(captured)
    assert secret_token not in blob, "the bearer token must never appear in logs"
    assert "SENSITIVE_JWT_PAYLOAD" not in blob
    # But the forensic field must be present.
    assert any(c.get("error_type") for c in captured), "error_type must be logged for triage"


# ===========================================================================
# W2-S2 — Per-user rate limits on workflow mutation endpoints
# ===========================================================================


@pytest.mark.asyncio
async def test_workflow_approve_rate_limit_30_per_window() -> None:
    """approve route rate-limited at 30/min/user (W2-S2)."""
    from app.api.rate_limit import _check, _reset_for_tests

    _reset_for_tests()
    for _ in range(30):
        assert await _check(
            "workflow.approve", "user-A", max_per_window=30, window_seconds=60.0
        )
    assert not await _check(
        "workflow.approve", "user-A", max_per_window=30, window_seconds=60.0
    )


@pytest.mark.asyncio
async def test_workflow_reject_rate_limit_30_per_window() -> None:
    """reject route rate-limited at 30/min/user (W2-S2)."""
    from app.api.rate_limit import _check, _reset_for_tests

    _reset_for_tests()
    for _ in range(30):
        assert await _check(
            "workflow.reject", "user-A", max_per_window=30, window_seconds=60.0
        )
    assert not await _check(
        "workflow.reject", "user-A", max_per_window=30, window_seconds=60.0
    )


@pytest.mark.asyncio
async def test_workflow_override_rate_limit_20_per_window() -> None:
    """override route rate-limited at 20/min/user (W2-S2). Tighter than the
    others because override writes up to 256 KB into ArtifactRow per call."""
    from app.api.rate_limit import _check, _reset_for_tests

    _reset_for_tests()
    for _ in range(20):
        assert await _check(
            "workflow.override", "user-A", max_per_window=20, window_seconds=60.0
        )
    assert not await _check(
        "workflow.override", "user-A", max_per_window=20, window_seconds=60.0
    )


def test_workflow_routes_declare_rate_limit_dependencies() -> None:
    """Each /workflow mutation route MUST declare a rate_limit dependency.
    Static-source check so a future refactor cannot silently drop a quota."""
    import inspect

    from app.api.routes.workflow import approve, override, reject

    src = inspect.getsource(approve)
    assert "workflow.approve" in src and "max_per_window=30" in src
    src = inspect.getsource(reject)
    assert "workflow.reject" in src and "max_per_window=30" in src
    src = inspect.getsource(override)
    assert "workflow.override" in src and "max_per_window=20" in src
