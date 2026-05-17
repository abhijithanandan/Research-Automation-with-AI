# Technical Specification — ResearchFlow AI

**Version:** 0.1 (Pre-MVP)
**Status:** Source of truth for implementation. Code must match this document. Update this document *before* changing a contract.

This is the **spec-driven development sheet**. Every endpoint, data model, event, and state transition the team will implement is captured here. Updates to a contract require a PR to this file with reviewer sign-off *before* implementation work begins.

---

## 1. Scope of this document

This document defines:

1. The canonical data model (Pydantic + SQL).
2. The REST API contract (mirrored in `docs/api/openapi.yaml`).
3. The WebSocket event contract.
4. The LangGraph state machine (nodes, edges, guards).
5. Per-agent input/output contracts.
6. The HITL approval protocol.
7. Versioning and change-management rules.

Anything not in this document is either a free implementation choice or a future feature (track in issues).

---

## 2. Core domain model

### 2.1 Entities

```
User ──< Project ──< WorkflowRun ──< AgentInvocation
                  ├──< Paper (approved pool)
                  ├──< Artifact (figures, tables, code, drafts)
                  └──< AuditLogEntry
```

### 2.2 Pydantic model definitions

These are the **wire** types. Persisted SQL types may add columns (created_at, etc.) but must serialize to / from these.

```python
# Identity
class User(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str | None = None
    created_at: datetime

# Project
class Project(BaseModel):
    id: UUID
    owner_id: UUID
    title: str
    seed_query: str
    output_format: Literal["markdown", "latex"] = "markdown"
    token_cap_usd: float = 5.0
    status: Literal["draft", "active", "completed", "archived"]
    current_phase: Phase
    created_at: datetime
    updated_at: datetime

class Phase(str, Enum):
    DISCOVERY = "discovery"
    SYNTHESIS = "synthesis"
    ANALYSIS = "analysis"       # v0.2
    DRAFTING = "drafting"
    DONE = "done"

# Workflow state
class WorkflowRun(BaseModel):
    id: UUID
    project_id: UUID
    phase: Phase
    state: Literal["running", "awaiting_approval", "approved", "rejected", "error"]
    checkpoint_id: str          # LangGraph checkpoint reference
    started_at: datetime
    awaiting_since: datetime | None
    last_event_at: datetime

# Papers (the approved pool)
class Paper(BaseModel):
    id: UUID
    project_id: UUID
    source: Literal["semantic_scholar", "arxiv", "crossref", "upload"]
    external_id: str            # DOI or arXiv ID
    title: str
    authors: list[str]
    year: int | None
    abstract: str | None
    pdf_url: HttpUrl | None
    citation_key: str           # BibTeX key — unique within project
    citation_count: int | None
    approved: bool = False
    added_at: datetime

# Critic / Scribe outputs
class Artifact(BaseModel):
    id: UUID
    project_id: UUID
    kind: Literal["matrix", "summary", "section", "figure", "code", "log"]
    label: str                  # e.g. "introduction", "results-fig-1"
    content: str                # markdown / latex / json-serialized / base64 for images
    mime_type: str
    produced_by: Literal["librarian", "critic", "analyst", "scribe", "human"]
    parent_id: UUID | None      # for revisions
    created_at: datetime

# Audit trail
class AuditLogEntry(BaseModel):
    id: UUID
    project_id: UUID
    workflow_run_id: UUID | None
    actor: Literal["system", "user", "librarian", "critic", "analyst", "scribe"]
    action: str                 # e.g. "approve", "reject", "edit", "agent.invoke"
    payload: dict
    model: str | None           # LLM model name where applicable
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    created_at: datetime
```

### 2.3 SQL schema (Postgres, illustrative)

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY,
    firebase_uid TEXT UNIQUE NOT NULL,
    email TEXT NOT NULL,
    display_name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE projects (
    id UUID PRIMARY KEY,
    owner_id UUID NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    seed_query TEXT NOT NULL,
    output_format TEXT NOT NULL DEFAULT 'markdown',
    token_cap_usd NUMERIC(10,2) NOT NULL DEFAULT 5.00,
    status TEXT NOT NULL DEFAULT 'draft',
    current_phase TEXT NOT NULL DEFAULT 'discovery',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workflow_runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    phase TEXT NOT NULL,
    state TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    awaiting_since TIMESTAMPTZ,
    last_event_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE papers (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    authors JSONB NOT NULL,
    year INT,
    abstract TEXT,
    pdf_url TEXT,
    citation_key TEXT NOT NULL,
    citation_count INT,
    approved BOOLEAN NOT NULL DEFAULT FALSE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, citation_key)
);

CREATE TABLE artifacts (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    produced_by TEXT NOT NULL,
    parent_id UUID REFERENCES artifacts(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workflow_run_id UUID REFERENCES workflow_runs(id),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    payload JSONB NOT NULL,
    model TEXT,
    tokens_in INT,
    tokens_out INT,
    cost_usd NUMERIC(10,4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_project ON audit_log(project_id, created_at DESC);
```

---

## 3. REST API contract

**Base URL (dev):** `http://localhost:8000/api/v1`
**Auth:** `Authorization: Bearer <firebase-id-token>` on every protected endpoint.
**Content-Type:** `application/json` unless otherwise noted.

The canonical machine-readable form lives in [`docs/api/openapi.yaml`](./docs/api/openapi.yaml). This section is the human-readable view and must stay in sync.

### 3.1 Health / meta

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Liveness probe. Returns `{ "status": "ok", "version": "..." }`. |
| GET | `/meta/providers` | Lists available LLM providers and current default. |

### 3.2 Projects

| Method | Path | Description |
| --- | --- | --- |
| POST | `/projects` | Create a project from a seed query. Returns the created `Project`. |
| GET | `/projects` | List the caller's projects. |
| GET | `/projects/{id}` | Get a project. |
| PATCH | `/projects/{id}` | Update title / token cap / output format. |
| DELETE | `/projects/{id}` | Archive (soft delete) a project. |

**POST /projects request**
```json
{
  "title": "Survey of HITL in agentic systems",
  "seed_query": "human-in-the-loop multi-agent LLM",
  "output_format": "markdown",
  "token_cap_usd": 5.0
}
```

### 3.3 Workflow control

| Method | Path | Description |
| --- | --- | --- |
| POST | `/projects/{id}/workflow/start` | Start (or resume) the workflow. Returns the active `WorkflowRun`. |
| GET | `/projects/{id}/workflow` | Get current run + phase + state. |
| POST | `/projects/{id}/workflow/approve` | Approve the pending phase and advance. |
| POST | `/projects/{id}/workflow/reject` | Reject; supply `{ "feedback": "..." }` to regenerate. |
| POST | `/projects/{id}/workflow/override` | Submit a manually-edited artifact in place of the agent output. |

**Approve / reject payload**
```json
{ "feedback": "Tighten Section 2; remove paragraph about transformers." }
```

**Override payload**
```json
{
  "artifact_kind": "section",
  "label": "introduction",
  "content": "## Introduction\n\nThe edited markdown...",
  "mime_type": "text/markdown"
}
```

### 3.4 Papers

| Method | Path | Description |
| --- | --- | --- |
| GET | `/projects/{id}/papers` | List candidate + approved papers. |
| POST | `/projects/{id}/papers/upload` | Multipart upload of a local PDF. Returns extracted metadata + a new `Paper`. |
| PATCH | `/projects/{id}/papers/{paper_id}` | Toggle approval, fix metadata, or override citation key. |
| DELETE | `/projects/{id}/papers/{paper_id}` | Remove from pool (only allowed before Phase 1 approval). |

### 3.5 Artifacts

| Method | Path | Description |
| --- | --- | --- |
| GET | `/projects/{id}/artifacts?kind={kind}` | List artifacts, optionally filtered. |
| GET | `/projects/{id}/artifacts/{artifact_id}` | Fetch a single artifact. |
| GET | `/projects/{id}/export?format=markdown\|latex\|bibtex` | Export the manuscript. |

### 3.6 Audit & telemetry

| Method | Path | Description |
| --- | --- | --- |
| GET | `/projects/{id}/audit` | Paginated audit log (newest first). |
| GET | `/projects/{id}/usage` | Token + cost rollup for the project. |

### 3.7 Error format

All errors return:
```json
{
  "error": {
    "code": "phase_locked",
    "message": "Cannot modify papers after Phase 1 approval.",
    "trace_id": "01HXYZ..."
  }
}
```
Standard codes: `unauthorized` (401), `forbidden` (403), `not_found` (404), `validation_error` (422), `phase_locked` (409), `token_cap_reached` (402), `provider_error` (502).

---

## 4. WebSocket event contract

**Endpoint:** `ws://<host>/api/v1/projects/{id}/events`
**Auth:** Send `{ "type": "auth", "token": "<firebase-id-token>" }` as the first message after connection. Server replies with `{ "type": "auth.ok" }` or closes with code 4401.

All messages are JSON. Every event has a `type` and `ts` (ISO-8601). Client-bound and server-bound events are namespaced.

### 4.1 Server → Client events

| `type` | Payload | When |
| --- | --- | --- |
| `state.changed` | `{ phase, state, run_id }` | Any workflow state transition. |
| `agent.started` | `{ agent, run_id }` | An agent begins work. |
| `agent.token` | `{ agent, run_id, delta }` | Streaming LLM token. High-frequency. |
| `agent.completed` | `{ agent, run_id, artifact_ids }` | An agent finishes. |
| `agent.error` | `{ agent, run_id, error }` | Agent failed (recoverable). |
| `approval.required` | `{ phase, run_id, summary }` | Engine has paused awaiting human input. |
| `usage.tick` | `{ tokens_in, tokens_out, cost_usd }` | Periodic usage rollup (≤ every 5s). |

### 4.2 Client → Server events

Most actions go via REST; the WS channel is mostly server→client. The only client-bound message after auth is heartbeat:

| `type` | Payload | When |
| --- | --- | --- |
| `ping` | `{}` | Every 30s. Server replies `pong`. |

---

## 5. State machine (LangGraph)

### 5.1 Nodes

| Node | Phase | Persona | Outputs |
| --- | --- | --- | --- |
| `discover` | Discovery | Librarian | Candidate `Paper[]` |
| `await_pool_approval` | Discovery | — | (gate) |
| `synthesize` | Synthesis | Critic | `matrix` + `summary` artifacts |
| `await_synthesis_approval` | Synthesis | — | (gate) |
| `analyze` (v0.2) | Analysis | Analyst | `code` + `figure` artifacts |
| `await_analysis_approval` (v0.2) | Analysis | — | (gate) |
| `draft_section` | Drafting | Scribe | `section` artifact (one section per pass) |
| `await_section_approval` | Drafting | — | (gate) |
| `assemble` | Drafting | — | Final manuscript artifact |
| `done` | Done | — | Terminal |

### 5.2 Edges & guards

```
START → discover → await_pool_approval
  approve:   → synthesize
  reject:    → discover (with feedback)

synthesize → await_synthesis_approval
  approve:   → (analyze if Phase 3 enabled, else draft_section)
  reject:    → synthesize (with feedback)
  override:  → (analyze | draft_section)

draft_section → await_section_approval
  approve & sections_remaining: → draft_section (next section)
  approve & sections_done:      → assemble
  reject:                       → draft_section (current, with feedback)
  override:                     → await_section_approval (next)

assemble → done
```

### 5.3 Gate invariants

- A gate node **must** persist its checkpoint before emitting `approval.required`.
- The gate **must not** advance on any input other than an authenticated approval/reject/override event.
- `manual_override` records the user-supplied artifact as the canonical output of the *preceding* node; the audit log records `produced_by: "human"`.
- Rejection always re-runs the *preceding* agent node with the feedback string injected into its prompt.

### 5.4 Persistence

LangGraph checkpoints are persisted to Postgres via the `langgraph-checkpoint-postgres` adapter. Each checkpoint is keyed by `(project_id, thread_id)` where `thread_id == workflow_run.id`.

---

## 6. Per-agent contracts

Full prose contracts live under `docs/agents/`. The summary here is the *signature* — input/output types only.

### 6.1 Librarian

```python
class LibrarianInput(BaseModel):
    seed_query: str
    max_candidates: int = 30
    sources: list[Literal["semantic_scholar", "arxiv", "crossref"]] = ["semantic_scholar", "arxiv"]

class LibrarianOutput(BaseModel):
    candidates: list[Paper]    # not yet approved
    expanded_queries: list[str]
    arxiv_categories: list[str]  # e.g. ["cs.CV", "cs.LG"] — used for targeted ArXiv queries
```

### 6.2 Critic

```python
class CriticInput(BaseModel):
    approved_papers: list[Paper]
    focus: str | None = None    # optional narrowing instruction from user
    feedback: str | None = None # populated on regeneration

class CriticOutput(BaseModel):
    matrix: Artifact            # kind="matrix"
    summary: Artifact           # kind="summary"
```

### 6.3 Analyst (v0.2)

```python
class AnalystInput(BaseModel):
    task_description: str
    dataset_refs: list[str]     # object-storage URIs
    feedback: str | None = None

class AnalystOutput(BaseModel):
    code: Artifact              # kind="code"
    figures: list[Artifact]     # kind="figure"
    log: Artifact               # kind="log"
```

### 6.4 Scribe

```python
class ScribeInput(BaseModel):
    section: Literal["abstract", "introduction", "related_work",
                     "methodology", "results", "discussion", "conclusion"]
    approved_pool: list[Paper]
    prior_sections: list[Artifact]
    output_format: Literal["markdown", "latex"]
    feedback: str | None = None

class ScribeOutput(BaseModel):
    section: Artifact           # kind="section"
    cited_keys: list[str]       # must be subset of approved_pool.citation_key
```

**Citation invariant:** `ScribeOutput.cited_keys` must be a subset of `{p.citation_key for p in approved_pool}`. A post-generation validator enforces this and forces a regeneration on violation.

---

## 7. Approval protocol

The HITL handshake is the system's most safety-critical mechanism. Implementation rules:

1. The gate node persists a checkpoint **before** broadcasting `approval.required`.
2. The REST `approve` / `reject` / `override` endpoints look up the active `WorkflowRun`, verify it is in state `awaiting_approval`, and dispatch the corresponding LangGraph command. Any other state returns 409 `phase_locked`.
3. Every approval action writes an `audit_log` entry with `actor: "user"` and the user's UID in the payload.
4. The frontend may not advance its local view until it receives a `state.changed` WS event reflecting the new phase. (No optimistic UI for gate transitions.)

---

## 8. Versioning & change management

- This file uses semantic versioning. Bump the version on every merged change.
- **Breaking changes** to the REST/WS contract require a `/v2` namespace; v1 is kept on a deprecation timer (≥ 60 days).
- **Adding** an optional field, a new event type, or a new endpoint is non-breaking.
- Every contract change requires:
  1. A PR to this file.
  2. A corresponding update to `docs/api/openapi.yaml`.
  3. One reviewer sign-off from the backend lead and one from the frontend lead.

---

## 9. Open contract questions

These are blocking decisions for the team. Resolve before implementation begins.

1. **Streaming format:** raw LLM token deltas vs. semantic chunk events. (Recommendation: raw deltas; UI assembles.)
2. **Checkpoint backend:** Postgres-only, or layered with Redis for in-flight runs?
3. **Auth at the WS layer:** first-message handshake (current spec) or `Sec-WebSocket-Protocol` token? (Current spec chosen because Firebase tokens are long.)
4. ~~**Citation key collision:**~~ **RESOLVED.** The first occurrence keeps the bare key (e.g. `smith2020`); subsequent collisions receive alphabetic suffixes (`smith2020a`, `smith2020b`, …). This is enforced inside `generate_citation_keys()` in `app/services/discovery.py` and verified by `test_librarian_generates_unique_citation_keys`. Faculty reviewers accepted this convention.
