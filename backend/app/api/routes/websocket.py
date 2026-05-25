"""WebSocket event endpoint. See SPEC.md §4."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.workflow import subscribe_project, unsubscribe_project
from app.utils.logging import get_logger

_log = get_logger(__name__)

router = APIRouter(tags=["websocket"])


# ---------------------------------------------------------------------------
# Per-IP WebSocket handshake rate limiting (audit round-4 LOW finding).
#
# An unauthenticated peer can repeatedly open WS connections; we MUST accept()
# before we can receive_json() the auth frame (Starlette protocol requirement).
# That gives an attacker a cheap path to soak server resources and force the
# auth path to keep doing JWT verification work.
#
# Defense: sliding-window per-IP rate limiter. The window is short and the
# budget is generous so well-behaved clients (page reloads, reconnects after
# brief outages) are never impacted, but a tight reconnect loop from one IP
# is rejected with WS code 4429 ("rate limited") before we read any frame.
#
# Single-process in-memory state is fine here because the limiter is a soft
# defense, not an authoritative quota — for multi-worker deployments add a
# shared store (Redis) as the next layer. For Phase 2 MVP this is sufficient.
# ---------------------------------------------------------------------------

_WS_RATE_WINDOW_SECONDS = 10.0
_WS_RATE_MAX_PER_WINDOW = 20  # 20 connections / 10s per IP — well above
# legitimate multi-tab + reconnect bursts, well below abuse cadence.
_ws_connect_timestamps: dict[str, deque[float]] = {}
_ws_rate_lock = asyncio.Lock()


async def _check_handshake_rate_limit(remote_ip: str) -> bool:
    """Returns True if the connection is within the budget, False if throttled.

    The lock keeps the sliding window correct under concurrent handshakes
    from the same IP. Critical for multi-worker async correctness, cheap in
    practice because the critical section is two deque ops.
    """
    now = time.monotonic()
    cutoff = now - _WS_RATE_WINDOW_SECONDS
    async with _ws_rate_lock:
        window = _ws_connect_timestamps.get(remote_ip)
        if window is None:
            window = deque()
            _ws_connect_timestamps[remote_ip] = window
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= _WS_RATE_MAX_PER_WINDOW:
            return False
        window.append(now)
        # Opportunistic GC: if some other IP's deque is empty after expiry,
        # drop it. Keeps the dict from growing unbounded on long uptimes.
        if len(_ws_connect_timestamps) > 1024:
            for ip in list(_ws_connect_timestamps.keys()):
                if not _ws_connect_timestamps[ip]:
                    del _ws_connect_timestamps[ip]
        return True


@router.websocket("/projects/{project_id}/events")
async def project_events(project_id: UUID, ws: WebSocket) -> None:
    """Stream workflow events to the client.

    Auth handshake: client sends `{"type":"auth","token":"<firebase-id-token>"}`.
    Server replies `{"type":"auth.ok"}` or closes with code 4401.

    After auth, server pushes events; client only sends `{"type":"ping"}`.
    """
    # ---- Per-IP rate limit (LOW-1) ---------------------------------------
    # Done after accept() because Starlette has no pre-accept reject path that
    # surfaces a sensible close code to the client. We accept then immediately
    # close with 4429 if the IP is over-budget — the connection survives just
    # long enough to communicate "slow down" to the client lib.
    remote_ip = ws.client.host if ws.client else "unknown"
    await ws.accept()
    if not await _check_handshake_rate_limit(remote_ip):
        _log.warning("ws_rate_limited", remote_ip=remote_ip)
        # 4429 mirrors HTTP 429; the frontend ws.ts treats anything outside
        # the no-reconnect set as retryable, so the client will back off.
        await ws.close(code=4429, reason="rate limited")
        return

    # ---- Auth handshake (SPEC §4) ----------------------------------------
    try:
        first_msg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
    except TimeoutError:
        await ws.close(code=4401, reason="auth timeout")
        return
    except (WebSocketDisconnect, ConnectionError, RuntimeError, ValueError) as exc:
        # Narrow set (MED-4): transport failures (Disconnect/ConnectionError),
        # send-on-closed-socket (RuntimeError), and malformed-JSON (ValueError
        # from receive_json). Log only the exception *type*: str(exc) on some
        # WebSocket frame errors can echo client-supplied payload fragments
        # into logs — information-disclosure risk on shared logging systems.
        _log.warning("ws_auth_recv_error", error_type=type(exc).__name__)
        await ws.close(code=4401, reason="auth error")
        return

    if first_msg.get("type") != "auth" or not first_msg.get("token"):
        await ws.close(code=4401, reason="expected auth message")
        return

    from app.api.deps import _stable_uuid_from_uid
    from app.db.session import get_session
    from app.models.db import ProjectRow
    from app.services.auth import verify_firebase_token

    try:
        claims = await verify_firebase_token(str(first_msg["token"]))
    except Exception as exc:
        # Firebase Admin raises a wide family of errors here
        # (InvalidIdTokenError, ExpiredIdTokenError, RevokedIdTokenError,
        # CertificateFetchError, ValueError on malformed input, plus network
        # errors fetching JWKs). We keep a broad catch — *any* failure means
        # we cannot trust this connection — but log the exception class so
        # ops can distinguish auth-rejection from auth-infrastructure-down.
        _log.warning("ws_auth_verify_failed", error_type=type(exc).__name__)
        await ws.close(code=4401, reason="invalid token")
        return

    uid = claims.get("uid")
    if not uid:
        await ws.close(code=4401, reason="invalid token")
        return

    user_id = _stable_uuid_from_uid(str(uid))

    async with get_session() as db:
        project = await db.get(ProjectRow, project_id)
        if project is None or project.owner_id != user_id:
            await ws.close(code=4403, reason="unauthorized for project")
            return

    await ws.send_json({"type": "auth.ok"})
    _log.info("ws_connected", project_id=str(project_id))

    # ---- Subscribe to project events -------------------------------------
    queue = subscribe_project(project_id)

    # Run two concurrent tasks: forwarding events and reading pings.
    # Narrow exception sets (MED-4): both loops should exit only on transport
    # failure modes (peer disconnect, half-closed socket, network error).
    # Anything else is a programming bug — let it propagate and surface in logs
    # rather than silently swallowing it inside an infinite loop.
    async def _send_events() -> None:
        while True:
            event = await queue.get()
            try:
                await ws.send_json(event)
            except (WebSocketDisconnect, ConnectionError, RuntimeError) as exc:
                # RuntimeError: Starlette raises this when sending on a closed socket.
                _log.info("ws_send_loop_exit", error_type=type(exc).__name__)
                break

    async def _read_pings() -> None:
        while True:
            try:
                msg = await ws.receive_json()
            except (WebSocketDisconnect, ConnectionError, RuntimeError) as exc:
                _log.info("ws_read_loop_exit", error_type=type(exc).__name__)
                break
            if msg.get("type") == "ping":
                try:
                    await ws.send_json({"type": "pong"})
                except (WebSocketDisconnect, ConnectionError, RuntimeError) as exc:
                    _log.info("ws_read_loop_exit", error_type=type(exc).__name__)
                    break

    sender = asyncio.create_task(_send_events())
    reader = asyncio.create_task(_read_pings())

    try:
        _done, pending = await asyncio.wait([sender, reader], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        # Pass the specific queue so multi-tab sessions are not broken.
        unsubscribe_project(project_id, queue)
        _log.info("ws_disconnected", project_id=str(project_id))
