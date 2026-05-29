"""FastAPI dependencies: auth, DB session, current user.

`CurrentUser` is the canonical dependency for every protected endpoint.
It verifies the Firebase ID token, upserts a UserRow so that owner_id FK
constraints resolve on real Postgres (M5 fix), and returns the User schema.

In dev (DEV_AUTH_BYPASS=true) the raw token string is used as firebase_uid.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import UserRow
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
    now = datetime.now(tz=UTC)

    # Identity model (MED — identity-derivation portability): `users.id` is a
    # DB-authoritative surrogate key (uuid4 generated on first insert), NOT a
    # value derived from the Firebase UID. `firebase_uid` is the natural key we
    # look up by. This decouples our internal identity from any external
    # provider's id format and from a derivation algorithm — so the audit trail
    # stays portable even if we change auth providers or namespaces later.
    #
    # Migration safety: existing rows created under the old UUIDv5 derivation
    # keep their ids untouched (we match on firebase_uid, which is unchanged),
    # so their projects/audit_log FKs remain valid. Only brand-new users get a
    # fresh uuid4 from the column default.
    existing = await db.scalar(select(UserRow).where(UserRow.firebase_uid == uid))
    if existing is None:
        row = UserRow(
            firebase_uid=uid,
            email=email,
            display_name=display_name,
            created_at=now,
        )
        db.add(row)
        # Flush so the DB-side uuid4 default materialises into row.id and is
        # visible before any route inserts that FK-reference users.id.
        await db.flush()
        await db.refresh(row)
        user_id = row.id
        created_at = row.created_at
    else:
        existing.email = email
        existing.display_name = display_name
        user_id = existing.id
        created_at = existing.created_at

    return User(
        id=user_id,
        email=email,
        display_name=display_name,
        created_at=created_at,
    )


CurrentUser = Annotated[User, Depends(get_current_user)]

# NOTE: the previous `_stable_uuid_from_uid` (UUIDv5-from-firebase-uid) was
# removed. `users.id` is now a DB-authoritative surrogate key (uuid4 column
# default) resolved by looking up `firebase_uid`. Identity is no longer
# coupled to a derivation algorithm — this keeps the audit trail portable
# across auth-provider or namespace changes (reviewer MED finding).
