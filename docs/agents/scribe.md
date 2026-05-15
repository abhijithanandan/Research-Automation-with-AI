# Agent Contract — The Scribe

**Phase:** 4 — Drafting
**Code:** `backend/app/agents/scribe.py`
**SPEC reference:** §6.4

## Responsibility

Draft one section of the manuscript at a time, using the approved paper pool as the only permitted citation source.

## I/O

```python
class ScribeInput:
    section: Literal["abstract", "introduction", "related_work",
                     "methodology", "results", "discussion", "conclusion"]
    approved_pool: list[Paper]
    prior_sections: list[Artifact]
    output_format: Literal["markdown", "latex"]
    feedback: str | None

class ScribeOutput:
    section: Artifact      # kind="section"
    cited_keys: list[str]  # subset of {p.citation_key for p in approved_pool}
```

## Behavior

1. **Context build.** Retrieve top-k passages from the vector store using a query derived from `section` + `feedback` (if any) + the last paragraph of the immediately-prior section (for coherence).
2. **Prompt.** Include a strict instruction: "Cite ONLY from the following BibTeX keys: ...". Pass the list inline. No URLs, no DOIs in the body — citation keys only.
3. **Stream.** Stream tokens through the WS channel as `agent.token` events.
4. **Validate.** Post-generation, extract every `[@key]` (markdown) or `\cite{key}` (LaTeX) reference and verify each is in the approved pool.
5. **Auto-retry.** On validation failure, regenerate **once** with the validation error injected as feedback. If the second attempt also fails, surface the error to the user via `agent.error`.

## Invariants

- **Citation invariant (hard).** `cited_keys ⊆ {p.citation_key for p in approved_pool}`. Enforced by a programmatic post-check, not by trust in the model.
- The section artifact's `mime_type` matches `output_format`: `text/markdown` or `text/x-latex`.
- `prior_sections` are *read-only* context; the Scribe does not modify them.

## Failure modes

| Failure | Behavior |
| --- | --- |
| Unknown citation key in output | Auto-retry once with the validator error appended as feedback; then surface to user. |
| Empty approved pool | Refuse to draft; surface `provider_error` — engine should not have reached this node. |
| LLM output too short / empty | Emit `agent.error`; user can retry. |

## Tests required

- `test_scribe_rejects_unknown_citation_keys` (already scaffolded in `tests/test_scribe_citation_validator.py`)
- `test_scribe_auto_retries_once_on_validation_failure`
- `test_scribe_streams_tokens`
- `test_scribe_respects_output_format`
