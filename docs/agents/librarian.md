# Agent Contract — The Librarian

**Phase:** 1 — Discovery
**Code:** `backend/app/agents/librarian.py`
**SPEC reference:** §6.1

## Responsibility

Given a seed query, return a ranked pool of candidate papers for the user to review.

## I/O

```python
class LibrarianInput:
    seed_query: str
    max_candidates: int = 30
    sources: list[Literal["semantic_scholar", "arxiv", "crossref"]]

class LibrarianOutput:
    candidates: list[Paper]      # not yet approved
    expanded_queries: list[str]
```

## Behavior

1. **Query expansion.** Use an LLM to derive 3–5 related query strings (synonyms, broader/narrower terms). Record them in `expanded_queries`.
2. **Source fan-out.** Issue the seed + expanded queries against every selected source in parallel.
3. **Deduplication.** Match by normalized DOI first, then by title fuzzy-match (token-set ratio ≥ 0.9).
4. **Ranking.** Combine source-provided relevance score with citation count and recency. Tie-break on title length.
5. **Trimming.** Return at most `max_candidates`.
6. **Citation keys.** Generate a unique BibTeX key per paper: `firstauthorlastnameYEAR`. Collisions disambiguated with `a`, `b`, ...

## Invariants

- Returned `Paper.approved` is always `false` — Phase 1's whole point is to let the user approve.
- The function never blocks on the user; it ends by handing control back to the gate node.
- Network calls go through `app.services.discovery` adapters; never call `httpx` directly from this agent.

## Failure modes

| Failure | Behavior |
| --- | --- |
| Source API 5xx | Skip that source for this run; emit `agent.error` (non-fatal). |
| Zero results from all sources | Return empty `candidates`; UI surfaces "no results — refine query". |
| LLM provider down (query expansion) | Fall back to using only the seed query. |

## Tests required

- `test_librarian_dedupes_by_doi`
- `test_librarian_dedupes_by_fuzzy_title`
- `test_librarian_generates_unique_citation_keys`
- `test_librarian_returns_at_most_max_candidates`
