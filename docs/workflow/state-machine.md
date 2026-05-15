# State Machine — Formal Specification

This document is the formal description of the LangGraph state machine. The Python implementation in `backend/app/graph/workflow.py` must match this exactly. See `SPEC.md §5` for the high-level summary.

## Nodes

| Node | Phase | Kind | Persona |
| --- | --- | --- | --- |
| `discover` | DISCOVERY | agent | Librarian |
| `await_pool_approval` | DISCOVERY | gate | — |
| `synthesize` | SYNTHESIS | agent | Critic |
| `await_synthesis_approval` | SYNTHESIS | gate | — |
| `analyze` (v0.2) | ANALYSIS | agent | Analyst |
| `await_analysis_approval` (v0.2) | ANALYSIS | gate | — |
| `draft_section` | DRAFTING | agent | Scribe |
| `await_section_approval` | DRAFTING | gate | — |
| `assemble` | DRAFTING | system | — |
| `done` | DONE | terminal | — |

## Diagram

```
START
  │
  ▼
┌────────────┐    approve     ┌───────────────────────────┐
│ discover   │───────────────►│ synthesize                │
│            │                │                           │
│  (cycles   │                │  (cycles on reject)       │
│  on reject)│                │                           │
└─────┬──────┘                └───────────┬───────────────┘
      │                                   │
      │ pause / approval.required         │ pause / approval.required
      ▼                                   ▼
[await_pool_approval]            [await_synthesis_approval]

                                          │ approve
                                          ▼
                              ┌───────────────────────────┐
                              │ (v0.2) analyze            │ ── pause ──► [await_analysis_approval]
                              └───────────┬───────────────┘
                                          │ approve
                                          ▼
┌──────────────────────┐  next section   ┌─────────────────────────┐
│ draft_section        │◄────────────────│ await_section_approval  │
│ (one section per run)│                 │  approve+done → assemble │
└─────────┬────────────┘                 └─────────────────────────┘
          │ pause
          ▼
[await_section_approval]
          │ approve & sections_remaining empty
          ▼
┌────────────┐
│ assemble   │
└─────┬──────┘
      │
      ▼
   [done]
```

## Events that drive transitions

A gate node consumes exactly one of these external events, dispatched from `/projects/{id}/workflow/{approve|reject|override}`:

| Event | Effect |
| --- | --- |
| `approve` | Resume; advance to the next agent node (or `assemble` if drafting is complete). |
| `reject(feedback)` | Re-enter the *preceding* agent node with `feedback` injected. |
| `override(artifact)` | Replace the preceding agent's output with the user-supplied artifact. Audit-log it as `produced_by: human`. Then behave as if `approve` was received. |

No other event can advance a gate. Anything else returns 409 `phase_locked`.

## Implementation notes

- Use LangGraph's `interrupt_before` to pin the gates. Resumption is via `graph.invoke(..., command=approve)` after applying the user's payload to the state.
- Persist the checkpoint **before** broadcasting `approval.required` on the WS channel. If the order is reversed, a fast client can race the persistence.
- The `assemble` node is *not* a gate; it runs straight after the final section approval and emits `state.changed` to `done`.

## Reject vs Override semantics

| | Reject | Override |
| --- | --- | --- |
| Inputs | feedback string | full artifact content |
| Re-runs agent | yes | no |
| Audit `produced_by` | unchanged (still agent) | `human` |
| Cost | another LLM call | free |

## Recovery

If the engine crashes mid-agent, on restart the workflow is re-entered at the last persisted checkpoint (which is *before* the agent ran). The agent re-executes with the same inputs. This is safe because agents are idempotent w.r.t. their inputs (we replace outputs rather than appending).
