"""Async SQLAlchemy engine and session factory.

All database access goes through `get_session`. The engine is created once
at startup (via the `lifespan` hook in `main.py`) and shared across requests.
Using asyncpg as the async driver per SPEC.md §2.3.

Transaction-ownership contract (audit round-3, MED-2):

  - The **session context manager** owns the *implicit* commit on clean exit.
    Request handlers that only mutate state without dispatching background
    work rely on this — they return normally, the manager commits, and the
    response flushes.

  - The service layer calls :func:`flush_for_background_dispatch` (NOT
    `session.commit()` directly) when it needs the DB write visible *before*
    spawning an :func:`asyncio.create_task` whose target uses a fresh
    session. The helper is a thin wrapper around ``session.commit()`` whose
    *name* documents the intent: this is the only legitimate reason to
    commit mid-handler, and the helper makes that explicit at every call
    site.

The previous mixed pattern (some sites called ``session.commit()`` directly,
others relied on the context manager) was correct but hard to reason about.
The wrapper turns the dual-commit "smell" into a single, named operation.
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
    """Provide a transactional async session.

    Commits on clean exit, rolls back on exception. Callers that need their
    changes visible **before** the context manager exits (e.g. to dispatch a
    background task) must use :func:`flush_for_background_dispatch`, not a
    bare ``session.commit()``.
    """
    async with _SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def flush_for_background_dispatch(session: AsyncSession) -> None:
    """Commit pending changes so a background task can read them.

    Use this *only* immediately before ``asyncio.create_task(...)`` when the
    spawned task opens its own session and depends on the in-flight row. The
    name is intentionally specific so future readers see why a commit
    happens mid-handler. Semantically equivalent to ``session.commit()``; the
    end-of-context auto-commit becomes a no-op after this is called.
    """
    await session.commit()


async def dispose_engine() -> None:
    """Dispose the connection pool — called in the lifespan shutdown hook."""
    await _engine.dispose()
