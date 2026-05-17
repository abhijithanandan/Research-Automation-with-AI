"""FastAPI dependencies: auth, DB session, current user.

`CurrentUser` is the canonical dependency for every protected endpoint.
It verifies the Firebase ID token and resolves the internal `User` schema.
In dev (DEV_AUTH_BYPASS=true), the raw token string is used as firebase_uid.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from app.models.schemas import User
from app.services.auth import verify_firebase_token


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Extract the Bearer token, verify it with Firebase, and return the user.

    The internal user representation is a lightweight schema object. Full DB
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


async def get_current_user(token: str = Depends(oauth2_scheme)) -> User:
    """Validate Firebase token and return/create the user."""
    from app.services.auth import verify_firebase_token

    try:
        claims = await verify_firebase_token(token)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid authentication credentials: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    uid: str = str(claims.get("uid", ""))
    email: str = str(claims.get("email", f"{uid}@unknown"))

    return User(
        # Stable UUID derived from firebase_uid — avoids a DB round-trip in the
        # auth dependency itself. The projects route handles the upsert.
        id=UUID(int=int.from_bytes(uid.encode()[:16].ljust(16, b"\x00"), "big")),
        email=email,
        display_name=str(claims.get("name")) if claims.get("name") else None,
        created_at=datetime.now(tz=UTC),
    )


CurrentUser = Annotated[User, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# DB session dependency (wired once sessions are needed in routes)
# ---------------------------------------------------------------------------


async def get_db_session():  # type: ignore[return]  # yielded, not returned
    """Yield an async SQLAlchemy session. Import lazily to avoid circular deps."""
    from app.db.session import get_session

    async with get_session() as session:
        yield session


# Convenience alias — routes annotate: `db: DbSession`

from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

DbSession = Annotated[AsyncSession, Depends(get_db_session)]

# Kept for backwards compat with any code that imported the old UUID sentinel.
_STUB_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _stable_uuid_from_uid(uid: str) -> UUID:
    """Deterministic UUID from a Firebase UID string (no DB lookup needed)."""
    raw = uid.encode()[:16].ljust(16, b"\x00")
    return UUID(int=int.from_bytes(raw, "big"))
