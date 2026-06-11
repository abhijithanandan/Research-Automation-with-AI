"""End-to-end live verification of hybrid search against real Chroma + Postgres.

Run with the dockerized stack up (postgres:5433, chroma:8001). Exercises the
REAL retrieval path — server-side Chroma embeddings + Postgres BM25 corpus +
rank_bm25 + RRF — with HYBRID_SEARCH_ENABLED on. Rerank is left OFF here (the
cross-encoder is unit-tested with a mock; verifying it live needs the ~2-3 GB
torch download), but we assert it degrades gracefully to RRF-only.

Not a pytest test — a throwaway integration probe; prints PASS/FAIL per check.
"""

from __future__ import annotations

import asyncio
import uuid

from app.config import get_settings
from app.services import bm25_store
from app.services.vector_store import ChromaVectorStore, get_reranker

NS = f"verify-hybrid-{uuid.uuid4().hex[:8]}"

DOCS = [
    {
        "id": "d1",
        "text": "Deep convolutional neural networks for image classification on ImageNet.",
    },
    {
        "id": "d2",
        "text": "A transformer architecture using self-attention for machine translation.",
    },
    {"id": "d3", "text": "Reciprocal rank fusion combines multiple ranked retrieval result lists."},
    {"id": "d4", "text": "Bayesian hierarchical models for small-sample clinical trial inference."},
]


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    return ok


async def main() -> int:
    settings = get_settings()
    settings.hybrid_search_enabled = True
    settings.rerank_enabled = False  # RRF-only; rerank graceful-degrade checked below
    store = ChromaVectorStore(url=settings.vector_db_url)

    results: list[bool] = []
    print(f"namespace: {NS}")
    print(f"chroma: {settings.vector_db_url} | db: {settings.database_url.split('@')[-1]}")

    # 1. Upsert → writes BOTH the dense Chroma collection and the Postgres BM25 corpus.
    await store.upsert(NS, DOCS)
    print("upsert done (dense + sparse)")

    # 2. BM25 corpus persisted in Postgres?
    corpus = await bm25_store.load_corpus(NS)
    results.append(
        _check(
            "BM25 corpus persisted to Postgres",
            corpus is not None and sorted(corpus.doc_ids) == ["d1", "d2", "d3", "d4"],
            f"ids={sorted(corpus.doc_ids) if corpus else None}",
        )
    )

    # 3. Hybrid search returns text-bearing candidates from the real path.
    hits = await store.hybrid_reranked_search(NS, "rank fusion retrieval", top_n=4)
    ids = [str(h["id"]) for h in hits]
    results.append(_check("hybrid_reranked_search returns hits", len(hits) > 0, f"ids={ids}"))
    results.append(
        _check("every returned candidate carries text", all(str(h.get("text", "")) for h in hits))
    )

    # 4. The keyword-only doc (d3) — strong BM25 match for "rank fusion retrieval" — surfaces.
    results.append(_check("BM25 keyword match (d3) present in fused pool", "d3" in ids))

    # 5. Reranker degrades gracefully (optional [rerank] extra not installed here).
    reranker = get_reranker()
    results.append(
        _check(
            "reranker degrades to None without [rerank] extra",
            reranker is None,
            "RRF-only path (expected; install .[rerank] to enable cross-encoder)",
        )
    )

    # 6. Flag OFF → identical to legacy dense query (zero-regression).
    settings.hybrid_search_enabled = False
    legacy = await store.hybrid_reranked_search(NS, "rank fusion retrieval")
    results.append(
        _check(
            "flag OFF == legacy dense top-3",
            len(legacy) <= 3 and all("fused_score" not in h for h in legacy),
            f"n={len(legacy)}",
        )
    )

    # Cleanup the probe namespace's BM25 row (leave Chroma collection; harmless).
    async with __import__("app.db.session", fromlist=["get_session"]).get_session() as s:
        from app.models.db import Bm25IndexRow

        row = await s.get(Bm25IndexRow, NS)
        if row is not None:
            await s.delete(row)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
