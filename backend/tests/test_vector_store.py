"""Tests for the ChromaDB vector store adapter (Phase 2 B-1).

docs/agents/critic.md §Behavior step 1 — the Critic embeds approved papers
via `vector_store.upsert`. The adapter must:
  1. upsert documents into a per-namespace collection.
  2. query a collection and return [{id, text, distance}].
  3. raise VectorStoreUnavailableError when ChromaDB is unreachable so the
     Critic can fall back to abstract-only extraction.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.vector_store import (
    ChromaVectorStore,
    VectorStoreUnavailableError,
    get_vector_store,
)


def _mock_chroma_client() -> MagicMock:
    """A MagicMock standing in for chromadb.HttpClient."""
    client = MagicMock()
    collection = MagicMock()
    client.get_or_create_collection.return_value = collection
    collection.count.return_value = 2
    collection.query.return_value = {
        "ids": [["doc-1", "doc-2"]],
        "documents": [["first abstract", "second abstract"]],
        "distances": [[0.1, 0.4]],
    }
    return client


@pytest.mark.asyncio
async def test_upsert_adds_documents_to_namespace_collection() -> None:
    """upsert must get-or-create a collection named by namespace and add docs."""
    client = _mock_chroma_client()
    store = ChromaVectorStore(url="http://localhost:8001")

    with patch.object(store, "_get_client", return_value=client):
        await store.upsert(
            namespace="project-abc",
            documents=[
                {"id": "doc-1", "text": "an abstract"},
                {"id": "doc-2", "text": "another abstract"},
            ],
        )

    client.get_or_create_collection.assert_called_once_with(name="project-abc")
    collection = client.get_or_create_collection.return_value
    collection.upsert.assert_called_once()
    kwargs = collection.upsert.call_args.kwargs
    assert kwargs["ids"] == ["doc-1", "doc-2"]
    assert kwargs["documents"] == ["an abstract", "another abstract"]


@pytest.mark.asyncio
async def test_query_returns_id_text_distance() -> None:
    """query must return a list of {id, text, distance} dicts."""
    client = _mock_chroma_client()
    store = ChromaVectorStore(url="http://localhost:8001")

    with patch.object(store, "_get_client", return_value=client):
        results = await store.query(namespace="project-abc", query="abstract", k=2)

    assert len(results) == 2
    assert results[0] == {"id": "doc-1", "text": "first abstract", "distance": 0.1}
    assert results[1] == {"id": "doc-2", "text": "second abstract", "distance": 0.4}


@pytest.mark.asyncio
async def test_upsert_raises_vector_store_unavailable_on_connection_error() -> None:
    """A connection failure must surface as VectorStoreUnavailableError, not a raw error."""
    store = ChromaVectorStore(url="http://localhost:8001")

    with patch.object(store, "_get_client", side_effect=ConnectionError("refused")):
        with pytest.raises(VectorStoreUnavailableError):
            await store.upsert(namespace="project-abc", documents=[{"id": "x", "text": "y"}])


@pytest.mark.asyncio
async def test_query_raises_vector_store_unavailable_on_connection_error() -> None:
    """query must also wrap connection errors as VectorStoreUnavailableError."""
    store = ChromaVectorStore(url="http://localhost:8001")

    with patch.object(store, "_get_client", side_effect=ConnectionError("refused")):
        with pytest.raises(VectorStoreUnavailableError):
            await store.query(namespace="project-abc", query="q", k=5)


def test_get_vector_store_is_singleton() -> None:
    """get_vector_store must return the same instance on repeated calls."""
    first = get_vector_store()
    second = get_vector_store()
    assert first is second
