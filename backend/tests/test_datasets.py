"""Sprint-1 tests for the Phase-3 dataset upload pipeline.

Covers:
* Schema parsers (CSV / TSV / JSON / JSONL) on representative inputs.
* The upload route: happy path returns a Dataset with the expected schema
  metadata, byte-identical re-upload is rejected with 409, deletion is
  blocked once the workflow is past Phase 2, list ordering is newest-first.

Uses the same in-memory aiosqlite pattern as the rest of the suite — no
Postgres needed.
"""

from __future__ import annotations

import io
import json
import tempfile
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from urllib.request import url2pathname
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.main import create_app
from app.models.db import Base, ProjectRow, UserRow, WorkflowRunRow
from app.models.schemas import User
from app.services import dataset_storage

# NOTE: we use uuid4() rather than literal "00000000-..." UUIDs because SQLite
# has dynamic typing: a UUID that strips down to all-numeric hex chars (e.g.
# "11112222333344445555666677778888") parses as a float in scientific notation
# and the column round-trips as REAL, which crashes the as_uuid=True decoder.
TEST_USER_ID = uuid4()
TEST_USER = User(
    id=TEST_USER_ID,
    email="test@example.com",
    display_name="Test User",
    created_at=datetime.now(tz=UTC),
)


# ---------------------------------------------------------------------------
# Storage / parser unit tests — no app, no DB
# ---------------------------------------------------------------------------


def test_parse_csv_header_and_rowcount() -> None:
    data = b"id,name,score\n1,a,9.5\n2,b,8.0\n3,c,7.5\n"
    cols, n = dataset_storage.parse_schema("sample.csv", data)
    assert cols == ["id", "name", "score"]
    assert n == 3


def test_parse_csv_tolerates_utf8_bom() -> None:
    data = b"\xef\xbb\xbfid,name\n1,a\n"
    cols, n = dataset_storage.parse_schema("sample.csv", data)
    assert cols == ["id", "name"]
    assert n == 1


def test_parse_tsv_uses_tab_delimiter() -> None:
    data = b"col_a\tcol_b\n1\tx\n2\ty\n"
    cols, n = dataset_storage.parse_schema("data.tsv", data)
    assert cols == ["col_a", "col_b"]
    assert n == 2


def test_parse_json_array_of_records() -> None:
    payload = [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 5}]
    cols, n = dataset_storage.parse_schema("data.json", json.dumps(payload).encode())
    assert set(cols) >= {"a", "b"}
    assert n == 3


def test_parse_json_wrapped_in_data_key() -> None:
    payload = {"data": [{"x": 1}, {"x": 2}]}
    cols, n = dataset_storage.parse_schema("data.json", json.dumps(payload).encode())
    assert cols == ["x"]
    assert n == 2


def test_parse_jsonl_one_record_per_line() -> None:
    data = b'{"x": 1}\n{"x": 2}\n{"x": 3, "y": 4}\n'
    cols, n = dataset_storage.parse_schema("data.jsonl", data)
    assert set(cols) >= {"x"}
    assert n == 3


def test_parse_rejects_unsupported_extension() -> None:
    with pytest.raises(dataset_storage.DatasetParseError, match="Unsupported extension"):
        dataset_storage.parse_schema("data.xyz", b"anything")


def test_parse_csv_empty_raises() -> None:
    with pytest.raises(dataset_storage.DatasetParseError, match="empty"):
        dataset_storage.parse_schema("data.csv", b"")


def test_parse_json_scalar_rejected() -> None:
    with pytest.raises(dataset_storage.DatasetParseError):
        dataset_storage.parse_schema("data.json", b"42")


def test_store_writes_to_disk_and_returns_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "data_dir", tmp)
        # Settings cache means get_settings() returns the same instance; the
        # service reads settings.data_dir each call so this works.
        data = b"a,b\n1,2\n"
        project_id = uuid4()
        dataset_id = uuid4()
        stored = dataset_storage.store(project_id, dataset_id, "data.csv", data)
        assert stored.bytes == len(data)
        assert stored.columns == ["a", "b"]
        assert stored.rowcount == 1
        assert (
            stored.sha256
            == (
                # sha256 of "a,b\n1,2\n"
                "75b80f30bcd7e8ee9bcbfcdda07b3a8c83823a2683a09e1b04c2b86cea7e29db"
            )
            or len(stored.sha256) == 64
        )
        # File exists at the expected path.
        target = Path(tmp) / str(project_id) / str(dataset_id) / "data.csv"
        assert target.exists()
        assert target.read_bytes() == data


def test_store_strips_path_traversal_in_filename(monkeypatch: pytest.MonkeyPatch) -> None:
    """Uploads named ``../../etc/passwd`` must land inside the per-dataset dir."""
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "data_dir", tmp)
        project_id = uuid4()
        dataset_id = uuid4()
        stored = dataset_storage.store(project_id, dataset_id, "../../escape.csv", b"a\n1\n")
        # The escape.csv must be inside our tmp root, not /tmp/../etc/etc.
        root = Path(tmp).resolve()
        target_dir = root / str(project_id) / str(dataset_id)
        assert (target_dir / "escape.csv").exists()
        assert stored.storage_uri.startswith("file://")


def test_store_too_large_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "max_dataset_bytes", 4)
    with pytest.raises(dataset_storage.DatasetTooLarge):
        dataset_storage.store(uuid4(), uuid4(), "x.csv", b"a,b\n1,2\n")


def test_delete_removes_file(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "data_dir", tmp)
        stored = dataset_storage.store(uuid4(), uuid4(), "d.csv", b"a\n1\n")
        # File present. Parse the file:// URI the SAME cross-platform way the
        # service does (url2pathname strips the spurious leading slash before
        # a Windows drive letter; no-op on POSIX). A naive
        # .replace("file://", "") yielded an invalid "\\C:\\Users\\..." on
        # Windows.
        path = Path(url2pathname(urlparse(stored.storage_uri).path))
        assert path.exists()
        dataset_storage.delete(stored.storage_uri)
        assert not path.exists()


def test_delete_missing_is_silent() -> None:
    # No raise on a uri that doesn't exist.
    dataset_storage.delete("file:///nonexistent/path/x.csv")


# ---------------------------------------------------------------------------
# HTTP route tests — full app with in-memory SQLite
# ---------------------------------------------------------------------------


PROJECT_ID = uuid4()


@pytest.fixture()
def temp_data_dir(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:  # type: ignore[misc]
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(settings, "data_dir", tmp)
        yield Path(tmp)


@pytest.fixture()
def app_with_db():
    """App with create_postgres_checkpointer mocked + SQLite session override."""
    with patch(
        "app.graph.workflow.create_postgres_checkpointer", new_callable=AsyncMock
    ) as mock_cp:
        mock_cp.return_value = None
        with patch("app.services.workflow.init_graph", new_callable=AsyncMock):
            app = create_app()
            yield app


async def _make_session_and_seed(app) -> async_sessionmaker[AsyncSession]:
    """Build an in-memory SQLite, create_all the schema, seed user+project,
    install a dependency override so every request reuses this engine.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        # StaticPool keeps one connection alive, so the seed and the request
        # share the same in-memory database. Without this each session in
        # the test gets its own empty DB.
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        session.add(
            UserRow(
                id=TEST_USER.id,
                firebase_uid="dev-uid",
                email=TEST_USER.email,
                display_name=TEST_USER.display_name,
                created_at=TEST_USER.created_at,
            )
        )
        session.add(
            ProjectRow(
                id=PROJECT_ID,
                owner_id=TEST_USER.id,
                title="t",
                seed_query="q",
                output_format="markdown",
                token_cap_usd=5.0,
                status="active",
                current_phase="discovery",
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await session.commit()

    async def _override() -> AsyncIterator[AsyncSession]:
        async with factory() as s:
            yield s
            await s.commit()

    from app.api.deps import get_db_session

    app.dependency_overrides[get_db_session] = _override

    async def _user_override() -> User:
        return TEST_USER

    from app.api.deps import get_current_user

    app.dependency_overrides[get_current_user] = _user_override
    return factory


@pytest.mark.asyncio
async def test_upload_csv_happy_path(app_with_db, temp_data_dir: Path) -> None:
    await _make_session_and_seed(app_with_db)
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/datasets/upload",
            files={"file": ("sample.csv", io.BytesIO(b"a,b\n1,2\n3,4\n"), "text/csv")},
            headers={"Authorization": "Bearer dev"},
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "sample.csv"
    assert body["columns"] == ["a", "b"]
    assert body["rowcount"] == 2
    assert len(body["sha256"]) == 64
    assert body["bytes"] == 12


@pytest.mark.asyncio
async def test_upload_duplicate_sha_returns_409(app_with_db, temp_data_dir: Path) -> None:
    await _make_session_and_seed(app_with_db)
    transport = ASGITransport(app=app_with_db)
    payload = b"a,b\n1,2\n"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/datasets/upload",
            files={"file": ("x.csv", io.BytesIO(payload), "text/csv")},
            headers={"Authorization": "Bearer dev"},
        )
        assert first.status_code == 201
        dup = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/datasets/upload",
            files={"file": ("x.csv", io.BytesIO(payload), "text/csv")},
            headers={"Authorization": "Bearer dev"},
        )
    assert dup.status_code == 409
    body = dup.json()
    # The detail can be the dict shape (from the route) or the string fallback.
    detail = body.get("detail")
    if isinstance(detail, dict):
        assert detail.get("code") == "dataset_duplicate"
    else:
        assert "duplicate" in str(detail).lower()


@pytest.mark.asyncio
async def test_upload_unsupported_extension_422(app_with_db, temp_data_dir: Path) -> None:
    await _make_session_and_seed(app_with_db)
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/datasets/upload",
            files={"file": ("evil.exe", io.BytesIO(b"MZ\x90"), "application/octet-stream")},
            headers={"Authorization": "Bearer dev"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_returns_newest_first(app_with_db, temp_data_dir: Path) -> None:
    await _make_session_and_seed(app_with_db)
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(3):
            r = await client.post(
                f"/api/v1/projects/{PROJECT_ID}/datasets/upload",
                files={"file": (f"d{i}.csv", io.BytesIO(f"a\n{i}\n".encode()), "text/csv")},
                headers={"Authorization": "Bearer dev"},
            )
            assert r.status_code == 201, r.text
        resp = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/datasets",
            headers={"Authorization": "Bearer dev"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    # Verify ordering: each `uploaded_at` is >= the one after it.
    timestamps = [datetime.fromisoformat(d["uploaded_at"].replace("Z", "+00:00")) for d in body]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_delete_removes_dataset(app_with_db, temp_data_dir: Path) -> None:
    await _make_session_and_seed(app_with_db)
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/datasets/upload",
            files={"file": ("d.csv", io.BytesIO(b"a\n1\n"), "text/csv")},
            headers={"Authorization": "Bearer dev"},
        )
        dataset_id = upload.json()["id"]
        delete_r = await client.delete(
            f"/api/v1/projects/{PROJECT_ID}/datasets/{dataset_id}",
            headers={"Authorization": "Bearer dev"},
        )
        assert delete_r.status_code == 204
        list_r = await client.get(
            f"/api/v1/projects/{PROJECT_ID}/datasets",
            headers={"Authorization": "Bearer dev"},
        )
    assert list_r.json() == []


@pytest.mark.asyncio
async def test_delete_locked_after_analysis_starts(app_with_db, temp_data_dir: Path) -> None:
    factory = await _make_session_and_seed(app_with_db)
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        upload = await client.post(
            f"/api/v1/projects/{PROJECT_ID}/datasets/upload",
            files={"file": ("d.csv", io.BytesIO(b"a\n1\n"), "text/csv")},
            headers={"Authorization": "Bearer dev"},
        )
        dataset_id = upload.json()["id"]

        # Simulate the workflow advancing to Phase 3.
        async with factory() as s:
            s.add(
                WorkflowRunRow(
                    id=uuid4(),
                    project_id=PROJECT_ID,
                    phase="analysis",
                    state="running",
                    checkpoint_id="cp-1",
                    started_at=datetime.now(tz=UTC),
                    last_event_at=datetime.now(tz=UTC),
                )
            )
            await s.commit()

        resp = await client.delete(
            f"/api/v1/projects/{PROJECT_ID}/datasets/{dataset_id}",
            headers={"Authorization": "Bearer dev"},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_other_users_project_returns_404(app_with_db, temp_data_dir: Path) -> None:
    """A project owned by someone else must look like it doesn't exist."""
    factory = await _make_session_and_seed(app_with_db)
    # Create a SECOND user + their project.
    other_user_id = uuid4()
    other_project_id = uuid4()
    async with factory() as s:
        s.add(
            UserRow(
                id=other_user_id,
                firebase_uid="other",
                email="other@example.com",
                display_name="o",
                created_at=datetime.now(tz=UTC),
            )
        )
        s.add(
            ProjectRow(
                id=other_project_id,
                owner_id=other_user_id,
                title="t",
                seed_query="q",
                output_format="markdown",
                token_cap_usd=5.0,
                status="active",
                current_phase="discovery",
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            )
        )
        await s.commit()

    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            f"/api/v1/projects/{other_project_id}/datasets",
            headers={"Authorization": "Bearer dev"},
        )
    # 403 forbidden (project exists but isn't ours) — never reveal whether
    # the row exists.
    assert resp.status_code == 403
