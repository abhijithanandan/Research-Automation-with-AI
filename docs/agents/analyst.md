# Agent Contract — The Analyst (v0.2)

**Phase:** 3 — Analysis
**Code:** `backend/app/agents/analyst.py`
**SPEC reference:** §6.3
**Status:** Scheduled for v0.2. Not part of the MVP.

## Responsibility

Given a task description and one or more dataset references, write Python code, execute it in a sandbox, and return generated figures, tables, and the execution log.

## I/O

```python
class AnalystInput:
    task_description: str
    dataset_refs: list[str]     # object-storage URIs
    feedback: str | None

class AnalystOutput:
    code: Artifact              # kind="code"
    figures: list[Artifact]     # kind="figure", base64-encoded PNG
    log: Artifact               # kind="log"
```

## Behavior

1. **Plan.** Use an LLM to produce a short plan in plain English. Surface the plan as a streaming preview.
2. **Code.** Generate a single Python script that imports `pandas`, `numpy`, `matplotlib`, optionally `scikit-learn`. No network calls.
3. **Execute.** Run the script in a sandboxed subprocess. Limits: 60s wall clock, 1 GiB RSS, no network, restricted FS (read-only access to dataset paths; writable `/tmp/out`).
4. **Capture.** Collect stdout/stderr (the log artifact), all `.png` files under `/tmp/out` (figure artifacts), and the script source (code artifact).

## Invariants

- The code artifact is **always returned to the user before execution proceeds further** in this node. In v0.2, the user gates execution by approving the code first. (Two-step interrupt: plan/code → approve → execute → review.)
- Sandbox escapes are treated as P0 security bugs.

## Failure modes

| Failure | Behavior |
| --- | --- |
| Script raises | Log the traceback as part of the log artifact; mark the node as `awaiting_approval` so the user can decide to regenerate or abort. |
| Time/memory limit hit | Return a partial log artifact; suggest a narrower task. |
| Dataset unreachable | Fail fast before execution; emit `agent.error`. |
