"""FastAPI dependencies: auth, DB session, current user.

`CurrentUser` is the canonical dependency for every protected endpoint.
It verifies the Firebase ID token, upserts a UserRow so that owner_id FK
constraints resolve on real Postgres (M5 fix), and returns the User schema.

In dev (DEV_AUTH_BYPASS=true) the raw token string is used as firebase_uid.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schemas import User
from app.services.auth import verify_firebase_token


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yield an async SQLAlchemy session. Import lazily to avoid circular deps."""
    from app.db.session import get_session

    async with get_session() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    db: DbSession = None,  # type: ignore[assignment]
) -> User:
    """Verify Firebase token, upsert UserRow, return User schema.

    The upsert ensures a users row exists before any project route runs an
    INSERT that references owner_id — without this the FK fails on Postgres.
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
        # M1-D: never log token content or full traceback frames here —
        # those frames carry the JWT through them. Class name + structured
        # security fields are enough for ops to triage.
        _log.warning(
            "http.auth.verify_failed",
            result="rejected",
            reason_code="verify_failed",
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    uid: str = str(claims.get("uid", ""))
    email: str = str(claims.get("email", f"{uid}@unknown"))
    display_name: str | None = str(claims.get("name")) if claims.get("name") else None

    # Identity resolution goes through the ONE shared rule in services.auth so
    # the HTTP and WebSocket transports can never drift (see the contract docs
    # on resolve_or_create_user). `users.id` is a DB surrogate, looked up by
    # firebase_uid — not derived from it.
    from app.services.auth import resolve_or_create_user

    row = await resolve_or_create_user(db, firebase_uid=uid, email=email, display_name=display_name)

    return User(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        created_at=row.created_at,
    )


CurrentUser = Annotated[User, Depends(get_current_user)]

# NOTE: the previous `_stable_uuid_from_uid` (UUIDv5-from-firebase-uid) was
# removed. `users.id` is now a DB-authoritative surrogate key (uuid4 column
# default) resolved by looking up `firebase_uid`. Identity is no longer
# coupled to a derivation algorithm — this keeps the audit trail portable
# across auth-provider or namespace changes (reviewer MED finding).
