"""FastAPI dependencies: auth, DB session, current user.

`CurrentUser` is the canonical dependency for every protected endpoint.
It verifies the Firebase ID token and resolves the internal `User` schema.
In dev (DEV_AUTH_BYPASS=true), the raw token string is used as firebase_uid.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import User
from app.services.auth import verify_firebase_token


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Extract the Bearer token, verify it with Firebase, and return the user.

    The internal user representation is a lightweight schema object. Full DB
    resolution (upsert users table) is deferred to the routes that need it;
    the auth dependency just validates identity.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[7:]  # strip "Bearer "

    try:
        claims = await verify_firebase_token(token)
    except Exception as exc:
        from app.utils.logging import get_logger

        _log = get_logger(__name__)
        _log.error("Authentication error validating token", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    uid: str = str(claims.get("uid", ""))
    email: str = str(claims.get("email", f"{uid}@unknown"))

    return User(
        id=_stable_uuid_from_uid(uid),
        email=email,
        display_name=str(claims.get("name")) if claims.get("name") else None,
        created_at=datetime.now(tz=UTC),
    )


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield an async SQLAlchemy session. Import lazily to avoid circular deps."""
    from app.db.session import get_session

    async with get_session() as session:
        yield session


# Convenience alias — routes annotate: `db: DbSession`

DbSession = Annotated[AsyncSession, Depends(get_db_session)]

# Kept for backwards compat with any code that imported the old UUID sentinel.
_STUB_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _stable_uuid_from_uid(uid: str) -> UUID:
    """Deterministic UUID from a Firebase UID string (no DB lookup needed)."""
    import hashlib

    digest = hashlib.sha256(uid.encode()).digest()
    return UUID(bytes=digest[:16])
