"""Postgres persistence for the Critic's BM25 sparse-retrieval corpus.

The hybrid-search path (``services.vector_store``) needs a keyword index that
survives backend restarts and stays in sync with the dense ChromaDB
collection. The backend has no persistent volume of its own (only Postgres and
Chroma do), so the corpus lives in Postgres as a JSONB row per namespace —
``{"doc_ids": [...], "doc_texts": [...]}`` — and the in-memory ``BM25Okapi`` is
rebuilt from the texts on load.

We persist the *raw chunk texts*, never a pickled BM25 object: rebuilding from
tokens is cheap and avoids a ``pickle.load`` bandit finding while keeping the
column portable (JSON on sqlite in tests, JSONB on Postgres).

All writes go through ``get_session()``; its context manager owns the commit,
so this module never calls ``session.commit()`` directly (forbidden-pattern
rule #2).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.session import get_session
from app.models.db import Bm25IndexRow
from app.utils.logging import get_logger

_log = get_logger(__name__)


class Bm25Corpus(BaseModel):
    """The serialised keyword corpus for one namespace.

    ``doc_ids`` and ``doc_texts`` are positionally aligned: ``doc_texts[i]`` is
    the chunk whose stable id is ``doc_ids[i]``.
    """

    doc_ids: list[str] = Field(default_factory=list)
    doc_texts: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.doc_ids


def _merge(existing: Bm25Corpus, doc_ids: list[str], doc_texts: list[str]) -> Bm25Corpus:
    """Upsert ``(id, text)`` pairs into ``existing`` by id, preserving order.

    New ids append; ids already present have their text replaced. Mirrors
    Chroma's ``upsert`` semantics so the sparse and dense indexes stay aligned.
    """
    index_by_id = {doc_id: i for i, doc_id in enumerate(existing.doc_ids)}
    ids = list(existing.doc_ids)
    texts = list(existing.doc_texts)
    for doc_id, text in zip(doc_ids, doc_texts, strict=True):
        pos = index_by_id.get(doc_id)
        if pos is None:
            index_by_id[doc_id] = len(ids)
            ids.append(doc_id)
            texts.append(text)
        else:
            texts[pos] = text
    return Bm25Corpus(doc_ids=ids, doc_texts=texts)


async def load_corpus(namespace: str) -> Bm25Corpus | None:
    """Return the persisted corpus for ``namespace``, or ``None`` if absent."""
    async with get_session() as session:
        row = await session.get(Bm25IndexRow, namespace)
        if row is None:
            return None
        return Bm25Corpus.model_validate(row.corpus)


async def upsert_documents(
    namespace: str,
    doc_ids: list[str],
    doc_texts: list[str],
) -> Bm25Corpus:
    """Merge ``(id, text)`` pairs into the namespace corpus and persist it.

    Returns the merged corpus. A namespace with no prior row is created. The
    write is an ``INSERT ... ON CONFLICT DO UPDATE`` so concurrent upserts for
    different namespaces never collide. Within the same namespace the row-level
    lock (``with_for_update=True``) ensures the read-modify-write is atomic —
    the second writer waits for the first to commit before reading the baseline.
    """
    async with get_session() as session:
        existing_row = await session.get(Bm25IndexRow, namespace, with_for_update=True)
        existing = (
            Bm25Corpus.model_validate(existing_row.corpus)
            if existing_row is not None
            else Bm25Corpus()
        )
        merged = _merge(existing, doc_ids, doc_texts)
        payload = merged.model_dump()
        now = datetime.now(tz=UTC)
        stmt = (
            pg_insert(Bm25IndexRow)
            .values(namespace=namespace, corpus=payload, updated_at=now)
            .on_conflict_do_update(
                index_elements=[Bm25IndexRow.namespace],
                set_={"corpus": payload, "updated_at": now},
            )
        )
        await session.execute(stmt)
    _log.info("bm25_corpus_persisted", namespace=namespace, docs=len(merged.doc_ids))
    return merged


async def list_namespaces() -> list[str]:
    """Return every namespace that has a persisted corpus (diagnostics/tests)."""
    async with get_session() as session:
        result = await session.execute(select(Bm25IndexRow.namespace))
        return [row[0] for row in result.all()]
