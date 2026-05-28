"""Per-actor sliding-window rate limiter (M1-C).

Reusable shape of the per-IP handshake limiter that already protects the
WebSocket auth path (``app/api/routes/websocket.py``). Exposed as a
FastAPI dependency factory so any route can opt in with a single line:

    @router.post(..., dependencies=[Depends(rate_limit("project.create", 30, 60))])

The actor is the authenticated ``user.id`` when available (so a misbehaving
single account is throttled even behind shared corporate IPs), falling back
to remote IP when the route hasn't resolved a user yet.

Single-process in-memory state — same caveat as the WS limiter: for a
multi-worker prod deployment, swap in Redis. For Phase 1/2/4 MVP this is
adequate.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request, status

from app.utils.logging import get_logger

_log = get_logger(__name__)

# Each bucket lives under (route_key, actor). Storing tuples in the dict
# keeps every route's quota independent — exhausting your /workflow/start
# budget doesn't block your /projects POST.
_buckets: dict[tuple[str, str], deque[float]] = {}
_lock = asyncio.Lock()

# Opportunistic GC threshold — same shape as the WS limiter (round-4 LOW-1
# + coderabbit GC fix). Drops buckets whose newest entry is outside the
# window so long-running processes don't leak memory.
_GC_THRESHOLD = 4096


async def _check(
    route_key: str,
    actor: str,
    max_per_window: int,
    window_seconds: float,
) -> bool:
    """Return True if the call is within budget, False if throttled."""
    now = time.monotonic()
    cutoff = now - window_seconds
    async with _lock:
        key = (route_key, actor)
        window = _buckets.get(key)
        if window is None:
            window = deque()
            _buckets[key] = window
        # Drop expired entries.
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= max_per_window:
            return False
        window.append(now)
        # Opportunistic GC — same shape as the WS limiter.
        if len(_buckets) > _GC_THRESHOLD:
            stale: list[tuple[str, str]] = []
            for k, q in _buckets.items():
                if not q or q[-1] < cutoff:
                    stale.append(k)
            for k in stale:
                del _buckets[k]
        return True


def rate_limit(
    route_key: str,
    max_per_window: int,
    window_seconds: float = 60.0,
) -> Callable[[Request], Awaitable[None]]:
    """Build a FastAPI dependency that throttles a route per actor.

    Args:
        route_key: short stable id for the route (e.g. ``project.create``).
            Different routes get independent quotas under the same actor.
        max_per_window: max successful calls per window. The (N+1)th call
            in the window raises HTTP 429.
        window_seconds: sliding-window size. Defaults to 60s — most use
            cases want "calls per minute".

    The actor is taken from ``request.state.user_id`` if a previous
    dependency populated it; otherwise the remote IP. This keeps the
    limiter useful both pre- and post-auth.
    """

    async def _dep(request: Request) -> None:
        # Actor preference: authenticated user > IP. We can't add CurrentUser
        # as a real dep here because that would force-import the auth chain
        # for every limited route; instead we read request.state which the
        # auth dep populates.
        actor = getattr(request.state, "user_id", None)
        if not actor:
            actor = request.client.host if request.client else "unknown"
        actor = str(actor)
        ok = await _check(route_key, actor, max_per_window, window_seconds)
        if not ok:
            _log.warning(
                "rate_limited",
                route=route_key,
                actor=actor,
                max_per_window=max_per_window,
                window_seconds=window_seconds,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "rate_limited",
                    "message": (
                        f"Too many requests to {route_key}. "
                        f"Limit: {max_per_window}/{int(window_seconds)}s."
                    ),
                },
            )

    return _dep


def _reset_for_tests() -> None:
    """Test-only hook — clear every bucket so tests don't bleed quota."""
    _buckets.clear()
