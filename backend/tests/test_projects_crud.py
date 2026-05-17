"""Tests for projects CRUD routes against an in-memory SQLite database.

Using SQLite (via aiosqlite) instead of Postgres keeps tests dependency-free.
The ORM models are compatible with both; the only difference is UUID handling
which we handle by casting to String for SQLite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import CurrentUser
from app.main import create_app
from app.models.db import Base
from app.models.schemas import User

# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

TEST_USER = User(
    id=UUID("00000000-0000-0000-0000-000000000001"),
    email="test@example.com",
    display_name="Test User",
    created_at=datetime.now(tz=UTC),
)


@pytest.fixture()
def app_with_mocked_graph():
    """Create the FastAPI app with graph initialisation skipped."""
    with patch(
        "app.graph.workflow.create_postgres_checkpointer", new_callable=AsyncMock
    ) as mock_cp:
        mock_cp.return_value.setup = AsyncMock()
        mock_cp.return_value.aclose = AsyncMock()
        with patch("app.services.workflow.init_graph", new_callable=AsyncMock):
            return create_app()


@pytest.fixture()
def override_auth(app_with_mocked_graph):
    """Override auth + db dependencies with test stubs."""
    app = app_with_mocked_graph

    # Create a fresh SQLite in-memory DB for each test.
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    import asyncio

    asyncio.get_event_loop().run_until_complete(create_tables())

    async def _get_test_session() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    async def _get_test_user() -> User:
        return TEST_USER

    app.dependency_overrides[CurrentUser.__class__] = _get_test_user  # type: ignore
    # Override the actual dependency functions
    from app.api import deps

    app.dependency_overrides[deps.get_current_user] = _get_test_user
    app.dependency_overrides[deps.get_db_session] = _get_test_session

    yield app

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_project(override_auth) -> None:
    """POST /projects should create and return a project."""
    app = override_auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/projects",
            json={
                "title": "Test Literature Review",
                "seed_query": "human in the loop AI agents",
                "output_format": "markdown",
                "token_cap_usd": 3.0,
            },
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["title"] == "Test Literature Review"
    assert data["status"] == "draft"
    assert data["current_phase"] == "discovery"


@pytest.mark.asyncio
async def test_list_projects_empty(override_auth) -> None:
    """GET /projects should return empty list for a new user."""
    app = override_auth
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/projects",
            headers={"Authorization": "Bearer test-token"},
        )

    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_unauthorized_without_token(app_with_mocked_graph) -> None:
    """Requests without a Bearer token must get 401.

    Uses the app WITHOUT the auth override so the real get_current_user
    dependency enforces the Authorization header requirement.
    """
    # Create a fresh app instance with no dependency overrides.
    app = app_with_mocked_graph
    # Clear the dependency overrides so real auth is in effect.
    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/projects")
        assert resp.status_code == 401
    finally:
        # Restore overrides for other tests.
        app.dependency_overrides.update(original_overrides)
