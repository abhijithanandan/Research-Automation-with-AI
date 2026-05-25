# Agent Contract — The Critic

**Phase:** 2 — Synthesis
**Code:** `backend/app/agents/critic.py`
**SPEC reference:** §6.2

## Responsibility

Given the approved paper pool, produce a structured comparison matrix and a narrative literature summary.

## I/O

```python
class CriticInput:
    approved_papers: list[Paper]
    focus: str | None
    feedback: str | None

class CriticOutput:
    matrix: Artifact     # kind="matrix",  mime="application/json"
    summary: Artifact    # kind="summary", mime="text/markdown"
```

## Behavior

1. **Embed.** For each approved paper not yet embedded, run `services.vector_store.upsert` with the abstract (and, if available, the full text).
2. **Extract per-paper attributes** via structured LLM call: `problem`, `method`, `dataset`, `key_findings`, `limitations`.
3. **Build matrix.** Serialize the per-paper extractions to JSON; produce a markdown table for display.
4. **Synthesize narrative.** Generate a 3–6 paragraph narrative summary, grouped by methodological cluster.
5. **Regeneration.** If `feedback` is non-empty, prepend it to the prompt with instruction "Apply the following revision instruction:".

## Invariants

- Every paper in `approved_papers` appears in `matrix`. No paper is silently dropped.
- The matrix JSON validates against [`docs/agents/critic.schema.json`](./critic.schema.json).
- The summary references at most the papers in `approved_papers` (no fabricated citations).

## Failure modes

| Failure | Behavior |
| --- | --- |
| LLM extraction fails on a paper | Mark that row as `extraction_failed` with the error; do not fail the whole node. |
| Empty approved pool | Return an `error` artifact and surface to user — the engine should not have reached this node. |

## Tests required

- `test_critic_matrix_contains_every_paper`
- `test_critic_regenerates_with_feedback`
- `test_critic_handles_extraction_failure_gracefully`
