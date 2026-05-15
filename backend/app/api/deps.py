"""FastAPI dependencies: auth, DB session, current user."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, status

from app.models.schemas import User


async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """Verify the Firebase ID token and return the resolved user.

    Stub implementation for boilerplate. Replace with real Firebase Admin SDK
    verification (see SPEC.md §6.1).
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")

    # TODO: firebase_admin.auth.verify_id_token(token) and map to internal user.
    return User(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        email="stub@example.com",
        display_name="Stub User",
        created_at=__import__("datetime").datetime.now(tz=__import__("datetime").UTC),
    )


CurrentUser = Annotated[User, Depends(get_current_user)]
