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
from uuid import UUID

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
    user_id = _stable_uuid_from_uid(uid)
    now = datetime.now(tz=UTC)

    # Upsert the users row so owner_id FK constraints resolve on real Postgres.
    existing = await db.scalar(select(UserRow).where(UserRow.firebase_uid == uid))
    if existing is None:
        db.add(
            UserRow(
                id=user_id,
                firebase_uid=uid,
                email=email,
                display_name=display_name,
                created_at=now,
            )
        )
        await db.flush()  # must be visible before any route inserts that FK-reference users.id
    else:
        existing.email = email
        existing.display_name = display_name

    return User(
        id=user_id,
        email=email,
        display_name=display_name,
        created_at=now,
    )


CurrentUser = Annotated[User, Depends(get_current_user)]

# Kept for backwards compat with any code that imported the old UUID sentinel.
_STUB_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

# Frozen namespace UUID for deriving ResearchFlow user IDs from Firebase UIDs.
# UUIDv5 within a stable namespace is the standard mechanism for "deterministic
# UUID from a name" — far easier to reason about than the previous SHA-256
# truncation, which was non-standard and made collision-space analysis fuzzy
# (MED-2 reviewer finding). The namespace itself is a frozen v4 UUID; treating
# it as the "researchflow.ai/users" domain keeps the mapping stable across
# deployments while domain-separating from other UUIDv5 uses in the project.
_RESEARCHFLOW_USER_NS = UUID("a3f12c14-7e51-4a89-9d3e-2b4f8c1c6e90")


def _stable_uuid_from_uid(uid: str) -> UUID:
    """Deterministic UUID from a Firebase UID string (no DB lookup needed).

    Returns a UUIDv5 within the ResearchFlow user namespace. Collisions across
    the ~2^122 effective space are vanishingly unlikely; uniqueness across
    deployments is guaranteed by the frozen namespace UUID.
    """
    from uuid import uuid5

    return uuid5(_RESEARCHFLOW_USER_NS, uid)
