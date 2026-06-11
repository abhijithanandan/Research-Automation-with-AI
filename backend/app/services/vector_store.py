"""Vector-store adapter. Default backend is Chroma in dev.

The Critic agent (Phase 2) embeds approved papers here and queries them for
RAG context. ChromaDB runs as a separate HTTP service; when it is unreachable
every operation raises `VectorStoreUnavailableError` so the Critic can fall back to
abstract-only extraction without hard-failing the workflow.

Hybrid search (opt-in via ``HYBRID_SEARCH_ENABLED``)
----------------------------------------------------
On top of the dense ChromaDB similarity search this module layers:

  1. **Sparse BM25 retrieval** — every upsert also indexes the chunk text into
     a per-namespace BM25 corpus persisted in Postgres (``services.bm25_store``).
  2. **Reciprocal Rank Fusion (RRF)** — the dense and sparse ranked lists are
     merged into one candidate pool with no score normalisation needed.
  3. **Cross-encoder reranking** (optional ``[rerank]`` extra) — the fused
     candidates are scored by ``BAAI/bge-reranker-base`` and the top-N returned.

The whole path is feature-flagged OFF by default: when disabled,
``hybrid_reranked_search`` is exactly the legacy dense ``query`` and no BM25
corpus is built, so existing behaviour is byte-for-byte unchanged.
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Protocol

from app.config import get_settings
from app.utils.logging import get_logger

if TYPE_CHECKING:
    from app.services.bm25_store import Bm25Corpus

_log = get_logger(__name__)

# Legacy dense fetch size — the count the Critic got before hybrid search
# existed. Used as the fallback k when HYBRID_SEARCH_ENABLED is off so the
# disabled path reproduces prior behaviour exactly.
_LEGACY_RAG_K = 3

_TOKEN_RE = re.compile(r"\w+")


def _tokenize(text: str) -> list[str]:
    """Lowercase word-token split used for both BM25 indexing and querying."""
    return _TOKEN_RE.findall(text.lower())


def reciprocal_rank_fusion(
    result_lists: list[list[tuple[str, float]]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    """Combine multiple ranked lists using Reciprocal Rank Fusion.

    Each input list is ``[(doc_id, score), ...]`` already sorted best-first;
    only the *rank position* is used (RRF needs no score normalisation), so the
    score field may be a placeholder. Returns the fused ranking as
    ``[(doc_id, fused_score), ...]`` sorted best-first.
    """
    if weights is None:
        weights = [1.0] * len(result_lists)
    scores: dict[str, float] = defaultdict(float)
    for result_list, weight in zip(result_lists, weights, strict=True):
        for rank, (doc_id, _score) in enumerate(result_list):
            scores[doc_id] += weight * (1.0 / (k + rank + 1))
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)


class BM25Index:
    """In-memory BM25Okapi index rebuilt from a persisted corpus.

    Construction tokenizes every document; ``query`` scores the whole corpus
    and returns the top ``k`` as ``[(doc_id, score), ...]``. An empty corpus
    (or one whose documents all tokenize to nothing) yields no results rather
    than raising.
    """

    def __init__(self, doc_ids: list[str], doc_texts: list[str]) -> None:
        self._doc_ids: list[str] = list(doc_ids)
        tokenized = [_tokenize(text) for text in doc_texts]
        self._bm25: Any = None
        if any(tokenized):
            from rank_bm25 import BM25Okapi

            self._bm25 = BM25Okapi(tokenized)

    def query(self, query: str, k: int) -> list[tuple[str, float]]:
        if self._bm25 is None:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(
            zip(self._doc_ids, scores, strict=True),
            key=lambda pair: float(pair[1]),
            reverse=True,
        )
        return [(doc_id, float(score)) for doc_id, score in ranked[:k]]


class CrossEncoderReranker:
    """Thin typed wrapper over a sentence-transformers ``CrossEncoder``.

    The model itself is untyped (``Any``); this class quarantines that and
    exposes a strictly-typed ``rerank`` so callers stay mypy-clean.
    """

    def __init__(self, model: Any) -> None:
        self._model = model

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, object]],
        top_n: int,
    ) -> list[dict[str, object]]:
        """Score ``(query, candidate_text)`` pairs and return the top ``top_n``.

        Each returned dict is a shallow copy of the input candidate with a
        ``rerank_score`` float added.
        """
        if not candidates:
            return []
        pairs = [(query, str(c.get("text", ""))) for c in candidates]
        raw_scores = self._model.predict(pairs)
        scored: list[tuple[dict[str, object], float]] = [
            (candidate, float(score))
            for candidate, score in zip(candidates, raw_scores, strict=True)
        ]
        scored.sort(key=lambda cs: cs[1], reverse=True)
        out: list[dict[str, object]] = []
        for candidate, score in scored[:top_n]:
            item = dict(candidate)
            item["rerank_score"] = score
            out.append(item)
        return out


# Module-level reranker cache. The model is heavy (~1.1 GB) so it is loaded at
# most once per process and only when reranking is actually used. ``_loaded``
# distinguishes "not tried yet" from "tried and unavailable" so a missing
# optional dependency logs exactly once.
_reranker: CrossEncoderReranker | None = None
_reranker_loaded = False


def get_reranker() -> CrossEncoderReranker | None:
    """Return the process-wide cross-encoder, or ``None`` if unavailable.

    Unavailable means either the optional ``[rerank]`` extra
    (sentence-transformers) is not installed or the model failed to load. In
    both cases the caller degrades to RRF-only rather than failing.
    """
    global _reranker, _reranker_loaded
    if _reranker_loaded:
        return _reranker
    _reranker_loaded = True
    try:
        from sentence_transformers import CrossEncoder
    except ImportError:
        _log.warning(
            "rerank_unavailable_missing_dependency",
            hint="install the optional [rerank] extra to enable cross-encoder reranking",
        )
        _reranker = None
        return None
    try:
        model = CrossEncoder(get_settings().rerank_model)
        _reranker = CrossEncoderReranker(model)
    except Exception as exc:  # model download / load failure → degrade, don't crash
        _log.warning("rerank_model_load_failed", error_type=type(exc).__name__, error=str(exc))
        _reranker = None
    return _reranker


class VectorStoreUnavailableError(RuntimeError):
    """Raised when the vector store backend cannot be reached."""


class VectorStore(Protocol):
    async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None: ...

    async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]: ...

    async def hybrid_reranked_search(
        self, namespace: str, query: str, *, top_n: int | None = None
    ) -> list[dict[str, object]]: ...


class ChromaVectorStore:
    """Chroma-backed vector store using chromadb's HTTP client.

    Collections are namespaced per project (the namespace is the project id).
    Chroma's default embedding function vectorizes documents server-side.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._client: Any = None

    def _get_client(self) -> Any:
        """Lazily build the chromadb HTTP client.

        Connection failures propagate as the raised exception; callers wrap
        them into VectorStoreUnavailableError.
        """
        if self._client is None:
            import chromadb

            host, port = _parse_url(self.url)
            # chromadb's HttpClient defaults to ssl=False; an https:// URL
            # would silently fall back to plaintext (coderabbit PR #5 finding).
            # Lift the scheme from the URL so https URLs actually use TLS.
            use_ssl = self.url.lower().startswith("https://")
            self._client = chromadb.HttpClient(host=host, port=port, ssl=use_ssl)
        return self._client

    async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None:
        """Add (or replace) documents in the collection for `namespace`."""
        if not documents:
            return
        try:
            await asyncio.to_thread(self._upsert_sync, namespace, documents)
        except VectorStoreUnavailableError:
            raise
        except Exception as exc:  # any backend error means the store is unavailable
            _log.warning(
                "vector_store_upsert_failed",
                namespace=namespace,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise VectorStoreUnavailableError(str(exc)) from exc

        # Hybrid search: mirror the same documents into the sparse BM25 corpus
        # so dense + keyword indexes stay aligned. Best-effort and OFF by
        # default — a BM25 persistence failure must not fail the dense embed
        # the Critic depends on, so `_index_bm25` swallows its own errors.
        if get_settings().hybrid_search_enabled:
            await self._index_bm25(namespace, documents)

    async def _index_bm25(self, namespace: str, documents: list[dict[str, object]]) -> None:
        """Persist the documents' text into the per-namespace BM25 corpus.

        Best-effort: any failure (DB down, etc.) is logged and swallowed so the
        already-succeeded dense upsert is not undone.
        """
        try:
            from app.services import bm25_store

            ids = [str(d["id"]) for d in documents]
            texts = [str(d["text"]) for d in documents]
            await bm25_store.upsert_documents(namespace, ids, texts)
        except Exception as exc:  # best-effort sparse index — never fail the embed
            _log.warning(
                "bm25_index_failed",
                namespace=namespace,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def _load_bm25_corpus(self, namespace: str) -> Bm25Corpus | None:
        """Load the persisted BM25 corpus, degrading to None on any failure."""
        try:
            from app.services import bm25_store

            return await bm25_store.load_corpus(namespace)
        except Exception as exc:  # sparse side is optional — degrade to dense-only
            _log.warning(
                "bm25_load_failed",
                namespace=namespace,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None

    async def hybrid_reranked_search(
        self,
        namespace: str,
        query: str,
        *,
        top_n: int | None = None,
    ) -> list[dict[str, object]]:
        """Dense + sparse retrieval fused with RRF, then optionally reranked.

        Returns ``[{id, text, ...}, ...]`` — the same shape the Critic already
        consumes from ``query`` (so the agent's prompt is untouched).

        When ``HYBRID_SEARCH_ENABLED`` is off this is exactly the legacy dense
        ``query`` (Zero-Regression). When on:

          1. dense top-``hybrid_dense_top_k`` from Chroma,
          2. sparse top-``hybrid_dense_top_k`` from the BM25 corpus,
          3. RRF fusion of the two rankings,
          4. cross-encoder rerank of the fused pool (if ``rerank_enabled`` and
             the model is available) — otherwise the RRF order is kept,
          5. the top ``top_n`` (default ``hybrid_top_n``) candidates.
        """
        settings = get_settings()
        if not settings.hybrid_search_enabled:
            return await self.query(namespace, query, k=top_n or _LEGACY_RAG_K)

        final_n = top_n or settings.hybrid_top_n
        dense_k = settings.hybrid_dense_top_k

        # 1. Dense. A Chroma outage propagates as VectorStoreUnavailableError,
        # exactly as the legacy path did, so the Critic's fallback still fires.
        dense = await self.query(namespace, query, k=dense_k)
        text_by_id: dict[str, str] = {str(d["id"]): str(d.get("text", "")) for d in dense}
        dense_ranked: list[tuple[str, float]] = [(str(d["id"]), 0.0) for d in dense]

        # 2. Sparse. Optional — a missing/empty corpus simply yields dense-only.
        sparse_ranked: list[tuple[str, float]] = []
        corpus = await self._load_bm25_corpus(namespace)
        if corpus is not None and not corpus.is_empty():
            for doc_id, text in zip(corpus.doc_ids, corpus.doc_texts, strict=True):
                text_by_id.setdefault(doc_id, text)
            sparse_ranked = BM25Index(corpus.doc_ids, corpus.doc_texts).query(query, k=dense_k)

        # 3. Fuse.
        fused = reciprocal_rank_fusion([dense_ranked, sparse_ranked], k=settings.hybrid_rrf_k)
        candidates: list[dict[str, object]] = [
            {"id": doc_id, "text": text_by_id.get(doc_id, ""), "fused_score": score}
            for doc_id, score in fused
            if text_by_id.get(doc_id)  # need text to rerank/return
        ]
        if not candidates:
            return []

        # 4. Rerank (optional) or 5. keep RRF order.
        if settings.rerank_enabled:
            reranker = get_reranker()
            if reranker is not None:
                return reranker.rerank(query, candidates[:dense_k], final_n)
        return candidates[:final_n]

    def _upsert_sync(self, namespace: str, documents: list[dict[str, object]]) -> None:
        client = self._get_client()
        collection = client.get_or_create_collection(name=namespace)
        ids = [str(d["id"]) for d in documents]
        texts = [str(d["text"]) for d in documents]
        collection.upsert(ids=ids, documents=texts)

    async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]:
        """Return the `k` nearest documents as [{id, text, distance}]."""
        try:
            return await asyncio.to_thread(self._query_sync, namespace, query, k)
        except VectorStoreUnavailableError:
            raise
        except Exception as exc:  # any backend error means the store is unavailable
            _log.warning(
                "vector_store_query_failed",
                namespace=namespace,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise VectorStoreUnavailableError(str(exc)) from exc

    def _query_sync(self, namespace: str, query: str, k: int) -> list[dict[str, object]]:
        client = self._get_client()
        collection = client.get_or_create_collection(name=namespace)
        count = collection.count()
        if count == 0:
            return []
        n = min(k, count)
        result = collection.query(query_texts=[query], n_results=n)
        ids = result.get("ids", [[]])[0]
        docs = result.get("documents", [[]])[0]
        distances = result.get("distances", [[]])[0]
        return [{"id": ids[i], "text": docs[i], "distance": distances[i]} for i in range(len(ids))]


_ALLOWED_VECTOR_SCHEMES = frozenset({"http", "https"})


def _parse_url(url: str) -> tuple[str, int]:
    """Split an http[s]://host[:port][/path] URL into (host, port).

    Uses :mod:`urllib.parse` rather than naive prefix-stripping so we handle
    IPv6 literals, credentials, paths, and missing schemes cleanly (audit
    round-3, MED-2). Returns sensible defaults when fields are missing.

    M3-B: rejects schemes outside ``{http, https}``. The vector store URL
    is loaded from VECTOR_DB_URL env var; a misconfiguration to
    ``file://etc/passwd`` or ``ftp://attacker.example.com`` should fail
    loud at parse time rather than silently downgrading to a default host.
    """
    from urllib.parse import urlparse

    # urlparse needs a scheme to interpret host correctly. If the caller
    # passed bare "host:port", prepend "http://" so it parses.
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_VECTOR_SCHEMES:
        raise ValueError(f"VECTOR_DB_URL must use http or https; got {scheme!r} (url={url!r}).")
    host = parsed.hostname or "localhost"
    # Default port: 8000 — matches Chroma's container default. Surfaces
    # explicit values from the URL otherwise.
    port = parsed.port if parsed.port is not None else 8000
    return host, port


# Module-level singleton — imported by the Critic agent.
_store: ChromaVectorStore | None = None


def get_vector_store() -> ChromaVectorStore:
    """Return the module-level vector store, creating it on first call."""
    global _store
    if _store is None:
        _store = ChromaVectorStore(url=get_settings().vector_db_url)
    return _store
