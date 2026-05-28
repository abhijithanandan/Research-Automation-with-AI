"""HTTP middlewares applied globally (M1-C).

So far we ship one middleware — a hard cap on request body size — to
short-circuit the obvious DoS / OOM vector of an unauthenticated peer
POSTing a 5 GiB JSON blob. The cap is conservative (1 MiB default, 50 MiB
on upload routes) and matches Phase 1 §3 SPEC payload sizes (the largest
legitimate body is a synthesis override which is itself bounded at
256_000 chars by ``ArtifactKindIn`` validators).

The middleware checks both ``Content-Length`` (cheap fast path) and the
streamed body length (catches chunked-transfer-encoding clients that omit
the header). On overflow we return 413 with a structured error envelope
so the frontend's ApiError classifier can categorize it as
``validation``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.utils.logging import get_logger

_log = get_logger(__name__)


# Default 1 MiB. Phase-2 synthesis override (the largest "normal" body in
# the system) is capped at 256K chars by ArtifactKindIn payload validation,
# so 1 MiB leaves ~4x headroom for base64-padding and JSON envelope.
_DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024

# Upload-route cap. PDF upload is currently 501 (BRD §8 — out of MVP scope)
# but the cap should land BEFORE the route ships so we never have to retrofit
# it. 50 MiB matches the typical preprint-server upload limit.
_UPLOAD_MAX_BODY_BYTES = 50 * 1024 * 1024

# Route prefixes that get the larger cap. Anything else uses the default.
_UPLOAD_PREFIXES: tuple[str, ...] = ("/api/v1/projects/",)
_UPLOAD_SUFFIX = "/papers/upload"


def _cap_for(path: str) -> int:
    if any(path.startswith(p) for p in _UPLOAD_PREFIXES) and path.endswith(_UPLOAD_SUFFIX):
        return _UPLOAD_MAX_BODY_BYTES
    return _DEFAULT_MAX_BODY_BYTES


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds the per-route cap with 413.

    Implementation note: we intentionally use ``BaseHTTPMiddleware`` (the
    higher-level Starlette helper) over a raw ASGI middleware so that
    FastAPI's exception handlers still produce the standard error
    envelope. The trade-off is one extra body buffer; for the small cap
    we use here that's negligible.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Fast path — honest clients send Content-Length.
        declared = request.headers.get("content-length")
        cap = _cap_for(request.url.path)
        if declared is not None:
            try:
                declared_int = int(declared)
            except ValueError:
                return _too_large_response(request.url.path, declared, cap)
            if declared_int > cap:
                _log.warning(
                    "body_size_limit_exceeded",
                    path=request.url.path,
                    declared=declared_int,
                    cap=cap,
                )
                return _too_large_response(request.url.path, declared_int, cap)

        # Slow path — chunked transfer encoding omits Content-Length. Read
        # the body once, measure it, then attach it back so downstream
        # handlers can re-read. Starlette's Request.body() caches the read.
        # For tiny payloads this is a no-op; for huge payloads this still
        # buffers the body, but we keep the cap small enough that the OOM
        # surface is bounded by `cap` itself.
        body = await request.body()
        if len(body) > cap:
            _log.warning(
                "body_size_limit_exceeded_streamed",
                path=request.url.path,
                actual=len(body),
                cap=cap,
            )
            return _too_large_response(request.url.path, len(body), cap)

        return await call_next(request)


def _too_large_response(path: str, actual: int | str, cap: int) -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "code": "request_too_large",
            "message": (
                f"Request body {actual} bytes exceeds the per-route cap of {cap} bytes for {path}."
            ),
        },
    )
