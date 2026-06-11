"""Tests for the ChromaDB vector store adapter (Phase 2 B-1).

docs/agents/critic.md §Behavior step 1 — the Critic embeds approved papers
via `vector_store.upsert`. The adapter must:
  1. upsert documents into a per-namespace collection.
  2. query a collection and return [{id, text, distance}].
  3. raise VectorStoreUnavailableError when ChromaDB is unreachable so the
     Critic can fall back to abstract-only extraction.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import get_settings
from app.services import vector_store as vs
from app.services.bm25_store import Bm25Corpus, _merge
from app.services.vector_store import (
    BM25Index,
    ChromaVectorStore,
    CrossEncoderReranker,
    VectorStoreUnavailableError,
    get_reranker,
    get_vector_store,
    reciprocal_rank_fusion,
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


# ---------------------------------------------------------------------------
# Hybrid search — Reciprocal Rank Fusion (pure)
# ---------------------------------------------------------------------------


def test_rrf_ranks_doc_present_in_both_lists_first() -> None:
    """A doc ranked highly by BOTH retrievers should win the fusion."""
    dense = [("d1", 0.0), ("d2", 0.0), ("d3", 0.0)]
    sparse = [("d2", 0.0), ("d4", 0.0)]
    fused = reciprocal_rank_fusion([dense, sparse], k=60)
    ids = [doc_id for doc_id, _ in fused]
    assert ids[0] == "d2"  # rank 2 in dense + rank 1 in sparse beats rank-1-only d1
    assert set(ids) == {"d1", "d2", "d3", "d4"}  # union, no drops


def test_rrf_weights_bias_toward_weighted_list() -> None:
    """Weighting one list higher promotes its top hit."""
    a = [("x", 0.0), ("y", 0.0)]
    b = [("y", 0.0), ("x", 0.0)]
    fused = reciprocal_rank_fusion([a, b], k=60, weights=[5.0, 1.0])
    assert fused[0][0] == "x"  # list a (weight 5) ranks x first


# ---------------------------------------------------------------------------
# Hybrid search — BM25 sparse index (real rank_bm25 on a tiny corpus)
# ---------------------------------------------------------------------------


def test_bm25_index_ranks_keyword_match_first() -> None:
    index = BM25Index(
        doc_ids=["d1", "d2", "d3"],
        doc_texts=[
            "deep learning neural networks for image classification",
            "reciprocal rank fusion hybrid search retrieval",
            "bayesian statistics and probability theory",
        ],
    )
    ranked = index.query("hybrid retrieval fusion", k=3)
    assert ranked[0][0] == "d2"
    assert ranked[0][1] > 0.0  # non-zero BM25 score for the match


def test_bm25_index_empty_corpus_returns_empty() -> None:
    assert BM25Index([], []).query("anything", k=5) == []


# ---------------------------------------------------------------------------
# Hybrid search — cross-encoder reranker (model mocked; no HF download)
# ---------------------------------------------------------------------------


class _FakeCrossEncoder:
    """Stand-in for sentence_transformers.CrossEncoder — fixed scores."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        assert len(pairs) == len(self._scores)
        return self._scores


def test_reranker_sorts_by_score_and_truncates() -> None:
    reranker = CrossEncoderReranker(_FakeCrossEncoder([0.1, 0.9, 0.5]))
    candidates = [
        {"id": "a", "text": "x"},
        {"id": "b", "text": "y"},
        {"id": "c", "text": "z"},
    ]
    out = reranker.rerank("q", candidates, top_n=2)
    assert [c["id"] for c in out] == ["b", "c"]  # 0.9, 0.5
    assert out[0]["rerank_score"] == 0.9


def test_reranker_empty_candidates_returns_empty() -> None:
    reranker = CrossEncoderReranker(_FakeCrossEncoder([]))
    assert reranker.rerank("q", [], top_n=5) == []


def test_get_reranker_degrades_when_dependency_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the optional [rerank] extra, get_reranker returns None (no crash)."""
    monkeypatch.setattr(vs, "_reranker", None)
    monkeypatch.setattr(vs, "_reranker_loaded", False)
    # Setting the module to None forces `import sentence_transformers` to raise
    # ImportError deterministically, whether or not it is installed locally.
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    assert get_reranker() is None


# ---------------------------------------------------------------------------
# Hybrid search — upsert BM25 hook (bm25 indexing mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_indexes_bm25_when_hybrid_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _mock_chroma_client()
    store = ChromaVectorStore(url="http://localhost:8001")
    monkeypatch.setattr(get_settings(), "hybrid_search_enabled", True)
    fake_upsert = AsyncMock()
    monkeypatch.setattr("app.services.bm25_store.upsert_documents", fake_upsert)

    with patch.object(store, "_get_client", return_value=client):
        await store.upsert(
            namespace="project-abc",
            documents=[{"id": "doc-1", "text": "alpha"}, {"id": "doc-2", "text": "beta"}],
        )

    fake_upsert.assert_awaited_once_with("project-abc", ["doc-1", "doc-2"], ["alpha", "beta"])


@pytest.mark.asyncio
async def test_upsert_skips_bm25_when_hybrid_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _mock_chroma_client()
    store = ChromaVectorStore(url="http://localhost:8001")
    monkeypatch.setattr(get_settings(), "hybrid_search_enabled", False)
    fake_upsert = AsyncMock()
    monkeypatch.setattr("app.services.bm25_store.upsert_documents", fake_upsert)

    with patch.object(store, "_get_client", return_value=client):
        await store.upsert(namespace="p", documents=[{"id": "doc-1", "text": "alpha"}])

    fake_upsert.assert_not_awaited()


# ---------------------------------------------------------------------------
# Hybrid search — orchestrator (dense + bm25 mocked, no DB / no model)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_search_disabled_is_legacy_dense_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChromaVectorStore(url="http://localhost:8001")
    monkeypatch.setattr(get_settings(), "hybrid_search_enabled", False)
    legacy = [{"id": "d1", "text": "t", "distance": 0.1}]
    monkeypatch.setattr(store, "query", AsyncMock(return_value=legacy))

    out = await store.hybrid_reranked_search(namespace="p", query="q")

    assert out == legacy
    store.query.assert_awaited_once_with("p", "q", k=3)  # _LEGACY_RAG_K


@pytest.mark.asyncio
async def test_hybrid_search_fuses_dense_and_sparse(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ChromaVectorStore(url="http://localhost:8001")
    settings = get_settings()
    monkeypatch.setattr(settings, "hybrid_search_enabled", True)
    monkeypatch.setattr(settings, "rerank_enabled", False)
    monkeypatch.setattr(settings, "hybrid_top_n", 3)

    # Dense finds d1, d2; sparse corpus additionally surfaces d3 by keyword.
    monkeypatch.setattr(
        store,
        "query",
        AsyncMock(return_value=[{"id": "d1", "text": "alpha"}, {"id": "d2", "text": "beta"}]),
    )
    corpus = Bm25Corpus(
        doc_ids=["d2", "d3"],
        doc_texts=["beta", "gamma keyword match"],
    )
    monkeypatch.setattr(store, "_load_bm25_corpus", AsyncMock(return_value=corpus))

    out = await store.hybrid_reranked_search(namespace="p", query="gamma keyword")

    ids = {c["id"] for c in out}
    assert "d3" in ids  # sparse-only doc made it into the fused pool
    assert all(c["text"] for c in out)  # every returned candidate carries text


@pytest.mark.asyncio
async def test_hybrid_search_applies_reranker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ChromaVectorStore(url="http://localhost:8001")
    settings = get_settings()
    monkeypatch.setattr(settings, "hybrid_search_enabled", True)
    monkeypatch.setattr(settings, "rerank_enabled", True)
    monkeypatch.setattr(settings, "hybrid_top_n", 2)

    monkeypatch.setattr(
        store,
        "query",
        AsyncMock(return_value=[{"id": "d1", "text": "alpha"}, {"id": "d2", "text": "beta"}]),
    )
    monkeypatch.setattr(store, "_load_bm25_corpus", AsyncMock(return_value=None))
    # Reranker flips the order: scores d1<d2 so d2 should come first.
    fake = CrossEncoderReranker(_FakeCrossEncoder([0.2, 0.8]))
    monkeypatch.setattr(vs, "get_reranker", lambda: fake)

    out = await store.hybrid_reranked_search(namespace="p", query="q")

    assert [c["id"] for c in out] == ["d2", "d1"]
    assert out[0]["rerank_score"] == 0.8


# ---------------------------------------------------------------------------
# BM25 corpus merge (pure)
# ---------------------------------------------------------------------------


def test_bm25_corpus_merge_appends_new_and_replaces_existing() -> None:
    existing = Bm25Corpus(doc_ids=["a", "b"], doc_texts=["old-a", "b-text"])
    merged = _merge(existing, doc_ids=["b", "c"], doc_texts=["new-b", "c-text"])
    assert merged.doc_ids == ["a", "b", "c"]  # b kept in place, c appended
    assert merged.doc_texts == ["old-a", "new-b", "c-text"]  # b's text replaced
