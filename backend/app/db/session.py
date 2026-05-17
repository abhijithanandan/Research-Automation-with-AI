"""Async SQLAlchemy engine and session factory.

All database access goes through `get_session`. The engine is created once
at startup (via the `lifespan` hook in `main.py`) and shared across requests.
Using asyncpg as the async driver per SPEC.md §2.3.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine = create_async_engine(
    get_settings().database_url,
    echo=False,
    pool_pre_ping=True,
)

_SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Provide a transactional async session. Rolls back on exception."""
    async with _SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Dispose the connection pool — called in the lifespan shutdown hook."""
    await _engine.dispose()
