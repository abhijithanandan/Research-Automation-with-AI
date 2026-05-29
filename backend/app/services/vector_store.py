"""Vector-store adapter. Default backend is Chroma in dev.

The Critic agent (Phase 2) embeds approved papers here and queries them for
RAG context. ChromaDB runs as a separate HTTP service; when it is unreachable
every operation raises `VectorStoreUnavailableError` so the Critic can fall back to
abstract-only extraction without hard-failing the workflow.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from app.config import get_settings
from app.utils.logging import get_logger

_log = get_logger(__name__)


class VectorStoreUnavailableError(RuntimeError):
    """Raised when the vector store backend cannot be reached."""


class VectorStore(Protocol):
    async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None: ...

    async def query(self, namespace: str, query: str, k: int = 10) -> list[dict[str, object]]: ...


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
