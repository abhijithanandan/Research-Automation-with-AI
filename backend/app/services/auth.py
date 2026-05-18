"""Firebase ID-token verification with DEV_AUTH_BYPASS support.

In production the Firebase Admin SDK verifies every ID token.
When DEV_AUTH_BYPASS=true (local dev only) the raw token string is treated as
the user's firebase_uid without verification — never set this in staging/prod.
"""

from __future__ import annotations

import asyncio
import json
import threading

import firebase_admin
from firebase_admin import auth, credentials

from app.config import get_settings
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
        _log.warning("dev_auth_bypass_active", uid=token[:16])
        return {"uid": token, "email": f"{token}@bypass.dev"}

    _ensure_firebase_initialized()
    decoded: dict[str, object] = await asyncio.to_thread(auth.verify_id_token, token)
    return decoded
