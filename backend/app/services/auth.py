"""Firebase ID-token verification."""

from __future__ import annotations

from app.config import get_settings


async def verify_firebase_token(token: str) -> dict[str, object]:
    """Verify a Firebase ID token and return its decoded claims.

    TODO: initialize firebase_admin once with credentials from settings; call
    `firebase_admin.auth.verify_id_token(token)`.
    """
    _ = token, get_settings()
    raise NotImplementedError("verify_firebase_token: wire up firebase_admin")
