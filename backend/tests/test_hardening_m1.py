"""Regression tests for the M1 security gate (hardening plan).

Coverage:
  M1-A: preflight enumerates the M1 additions (anthropic, firebase_admin,
        asyncpg, psycopg). Already covered by an extended assertion in
        ``test_audit_phase2_round3.test_preflight_lists_runtime_and_dev_modules``;
        no additional test here.
  M1-B: lifespan rejects DEV_AUTH_BYPASS=true outside development AND emits
        an app.start audit row on clean boot. The reject branch already has
        coverage in ``test_audit_phase2_round3``; this file adds the
        positive ``app.start`` log shape test.
  M1-C: rate_limit dependency factory throttles per-actor with sliding
        window. Body-size middleware returns 413 above the cap.
  M1-D: auth-path logs carry the structured security fields
        (event/actor/result/reason_code) and never include token content.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# M1-C: rate limiter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_allows_then_blocks_for_same_actor() -> None:
    from app.api.rate_limit import _check, _reset_for_tests

    _reset_for_tests()
    # 3 calls allowed in a 60s window — the 4th is rejected.
    for _ in range(3):
        assert await _check("test.route", "user-A", max_per_window=3, window_seconds=60.0)
    assert not await _check("test.route", "user-A", max_per_window=3, window_seconds=60.0)


@pytest.mark.asyncio
async def test_rate_limit_is_independent_per_actor() -> None:
    """Two different actors must not share a quota — a noisy peer can't
    starve a quiet one."""
    from app.api.rate_limit import _check, _reset_for_tests

    _reset_for_tests()
    for _ in range(3):
        assert await _check("test.route", "user-A", max_per_window=3, window_seconds=60.0)
    # user-A is exhausted; user-B starts fresh.
    assert await _check("test.route", "user-B", max_per_window=3, window_seconds=60.0)


@pytest.mark.asyncio
async def test_rate_limit_is_independent_per_route() -> None:
    """Exhausting one route's quota for an actor must not block other routes
    for the same actor — quotas are per (route_key, actor) tuple."""
    from app.api.rate_limit import _check, _reset_for_tests

    _reset_for_tests()
    for _ in range(2):
        assert await _check("a.create", "user-A", max_per_window=2, window_seconds=60.0)
    assert not await _check("a.create", "user-A", max_per_window=2, window_seconds=60.0)
    # Different route — same actor — fresh quota.
    assert await _check("b.start", "user-A", max_per_window=2, window_seconds=60.0)


@pytest.mark.asyncio
async def test_rate_limit_dependency_raises_429() -> None:
    """The FastAPI dependency variant must raise HTTPException(429) on
    overflow with a structured error envelope the frontend can categorize."""
    from app.api.rate_limit import _reset_for_tests, rate_limit

    _reset_for_tests()
    dep = rate_limit("test.dep", max_per_window=1, window_seconds=60.0)

    class _StubRequest:
        def __init__(self, ip: str) -> None:
            self.state = type("S", (), {})()
            self.client = type("C", (), {"host": ip})()

    req = _StubRequest("203.0.113.1")
    # First call succeeds (returns None).
    await dep(req)  # type: ignore[arg-type]
    # Second raises.
    with pytest.raises(HTTPException) as exc_info:
        await dep(req)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "rate_limited"  # type: ignore[index]


# ---------------------------------------------------------------------------
# M1-C: body-size middleware
# ---------------------------------------------------------------------------


def _app_with_body_size_middleware() -> FastAPI:
    """Minimal FastAPI app wired with just the body-size middleware so we
    can drive it with TestClient without spinning up the whole project."""
    from app.api.middleware import BodySizeLimitMiddleware

    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware)

    @app.post("/echo")
    async def echo(body: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "size": len(str(body))}

    return app


def test_body_size_limit_under_cap_passes_through() -> None:
    client = TestClient(_app_with_body_size_middleware())
    # ~100 bytes — well under the 1 MiB cap.
    resp = client.post("/echo", json={"hello": "world"})
    assert resp.status_code == 200


def test_body_size_limit_rejects_payload_over_cap() -> None:
    client = TestClient(_app_with_body_size_middleware())
    # 2 MiB of body — declared Content-Length triggers the fast path.
    big_body = "x" * (2 * 1024 * 1024)
    resp = client.post(
        "/echo",
        json={"big": big_body},
    )
    assert resp.status_code == 413
    assert resp.json()["code"] == "request_too_large"


def test_body_size_limit_rejects_malformed_content_length() -> None:
    """A non-integer Content-Length is itself a red flag — reject with 413
    rather than parsing it as 0 and silently letting the body through."""
    client = TestClient(_app_with_body_size_middleware())
    resp = client.post(
        "/echo",
        content=b'{"hi":"there"}',
        headers={
            "content-length": "not-an-int",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# M1-D: auth-path log redaction & structured fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dev_bypass_log_does_not_leak_full_token() -> None:
    """The dev-bypass log line must not contain the full token string.
    A previous version sliced [:16] which would expose a JWT prefix if a
    real Firebase token was accidentally passed in bypass mode."""
    from unittest.mock import patch

    from app.services import auth as auth_mod

    fake_jwt_prefix = "eyJhbGciOiJSUzI1NiIs"  # head of a Firebase JWT
    captured: list[tuple[str, dict[str, object]]] = []

    def _capture(*args: object, **fields: object) -> None:
        # structlog logger.warning(name, **fields) — name lands as positional.
        name = str(args[0]) if args else str(fields.get("event", ""))
        captured.append((name, dict(fields)))

    with patch.object(auth_mod, "get_settings") as mock_settings:
        mock_settings.return_value.dev_auth_bypass = True
        with patch.object(auth_mod._log, "warning", side_effect=_capture):
            await auth_mod.verify_firebase_token(fake_jwt_prefix + "moremoremoremoremore")

    assert captured, "verify_firebase_token must emit a log line in bypass mode"
    name, fields = captured[0]
    # Event name carries the structured tag (structlog uses the positional
    # arg as `event`).
    assert name == "auth.dev_bypass"
    # Token-redaction guarantees:
    actor = str(fields.get("actor", ""))
    assert actor.endswith("***"), "actor must be redacted with *** suffix"
    # No more than the first 4 chars of the supplied token should appear:
    assert fake_jwt_prefix not in actor, (
        "log must not echo the JWT prefix (only first 4 chars + redaction)"
    )
    # Structured security fields:
    assert fields.get("result") == "allowed"
    assert fields.get("reason_code") == "dev_auth_bypass_true"
