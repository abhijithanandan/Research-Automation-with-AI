"""Firebase ID-token verification with DEV_AUTH_BYPASS support.

In production the Firebase Admin SDK verifies every ID token.
When DEV_AUTH_BYPASS=true (local dev only) the raw token string is treated as
the user's firebase_uid without verification — never set this in staging/prod.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from uuid import UUID

import firebase_admin
from firebase_admin import auth, credentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.db import UserRow
from app.utils.logging import get_logger

_log = get_logger(__name__)

_firebase_initialized = False
_firebase_lock = threading.Lock()


def _ensure_firebase_initialized() -> None:
    """Initialize the Firebase Admin SDK exactly once (idempotent)."""
    global _firebase_initialized
    if _firebase_initialized:
        return

    with _firebase_lock:
        if _firebase_initialized:
            return

        settings = get_settings()

        if settings.firebase_credentials_json:
            cred_dict = json.loads(settings.firebase_credentials_json)
            cred = credentials.Certificate(cred_dict)
        elif settings.firebase_credentials_path:
            cred = credentials.Certificate(settings.firebase_credentials_path)
        else:
            # Application Default Credentials (GCP/Cloud Run environments).
            cred = credentials.ApplicationDefault()

        firebase_admin.initialize_app(cred, {"projectId": settings.firebase_project_id})
        _firebase_initialized = True
        _log.info("firebase_initialized", project_id=settings.firebase_project_id)


async def verify_firebase_token(token: str) -> dict[str, object]:
    """Verify a Firebase ID token and return its decoded claims.

    If DEV_AUTH_BYPASS is enabled the token is assumed to be the firebase_uid
    directly — use only in local development.
    """
    settings = get_settings()

    if settings.dev_auth_bypass:
        # M1-D: log only the first 4 chars + redaction marker. In bypass
        # mode the "token" is normally a short dev UID, but if anyone
        # accidentally pastes a real Firebase JWT here, slicing 16 chars
        # would still leak a prefix. 4 chars is enough to disambiguate
        # two dev users in logs without leaking JWT structure. structlog
        # reserves the positional first arg as the event name, so use it
        # as the structured event id directly.
        _log.warning(
            "auth.dev_bypass",
            actor=token[:4] + "***",
            result="allowed",
            reason_code="dev_auth_bypass_true",
        )
        return {"uid": token, "email": f"{token}@bypass.dev"}

    _ensure_firebase_initialized()
    decoded: dict[str, object] = await asyncio.to_thread(auth.verify_id_token, token)
    return decoded


# ---------------------------------------------------------------------------
# Canonical internal-user resolution — the ONE rule both HTTP and WS use.
# ---------------------------------------------------------------------------
#
# Identity contract (single source of truth):
#   * `users.firebase_uid` is the NATURAL key (the external identity).
#   * `users.id` is a DB-authoritative surrogate (uuid4, column default) —
#     never derived from the firebase_uid. This decouples our internal
#     identity from any auth provider's id format and keeps the audit trail
#     portable across provider/namespace changes.
#   * To go from a verified firebase_uid to our internal user id you LOOK UP
#     the row (never recompute). The HTTP path may CREATE the row on first
#     sight; the WS path is read-only (a connection for a user who has never
#     hit an HTTP route owns no projects, so a miss is an authz failure).
#
# Both `resolve_or_create_user` (HTTP) and `resolve_user_id` (WS) live here so
# the two transports can never drift on how identity is resolved.


async def resolve_or_create_user(
    db: AsyncSession,
    *,
    firebase_uid: str,
    email: str,
    display_name: str | None,
) -> UserRow:
    """Return the UserRow for `firebase_uid`, creating it on first sight.

    Used by the HTTP auth dependency. On create, `users.id` is assigned by the
    DB column default (uuid4); on existing, email/display_name are refreshed.
    Flushes so the row (and its id) is visible to FK-referencing inserts in the
    same request.
    """
    existing = await db.scalar(select(UserRow).where(UserRow.firebase_uid == firebase_uid))
    if existing is not None:
        existing.email = email
        existing.display_name = display_name
        return existing

    row = UserRow(
        firebase_uid=firebase_uid,
        email=email,
        display_name=display_name,
        created_at=datetime.now(tz=UTC),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def resolve_user_id(db: AsyncSession, firebase_uid: str) -> UUID | None:
    """Return the internal user id for `firebase_uid`, or None if no row exists.

    Read-only — used by the WebSocket handshake. Returns None rather than
    creating a row: a WS connection authenticates an *existing* user against an
    *existing* project; a missing users row means the token's owner has no
    resources here, which the caller treats as an authorization failure.
    """
    row = await db.scalar(select(UserRow).where(UserRow.firebase_uid == firebase_uid))
    return row.id if row is not None else None
