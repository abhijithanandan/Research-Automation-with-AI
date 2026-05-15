"""Vector-store adapter. Default backend is Chroma in dev."""

from __future__ import annotations

from typing import Protocol


class VectorStore(Protocol):
    async def upsert(
        self, namespace: str, documents: list[dict[str, object]]
    ) -> None: ...

    async def query(
        self, namespace: str, query: str, k: int = 10
    ) -> list[dict[str, object]]: ...


class ChromaVectorStore:
    """Chroma-backed vector store. TODO: wire up chromadb HTTP client."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def upsert(self, namespace: str, documents: list[dict[str, object]]) -> None:
        _ = namespace, documents
        raise NotImplementedError

    async def query(
        self, namespace: str, query: str, k: int = 10
    ) -> list[dict[str, object]]:
        _ = namespace, query, k
        raise NotImplementedError
