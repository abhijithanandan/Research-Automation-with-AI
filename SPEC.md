# Technical Specification — ResearchFlow AI

**Version:** 0.3 (v0.2 milestone) — activates the Phase-3 Analyst pipeline (FR-2.3) with the dataset upload routes, two new HITL gates (`analysis.code` + `analysis.results`) and dedicated approve/reject endpoints, the `datasets` table, and the `gate=code|results` discriminator on Phase-3 `approval.required` WS events. All additions are additive and non-breaking; v0.2 (Phase-4 Feature Pack) contract is unchanged.
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

# Phase 3 input (FR-2.3, SPEC v0.3 addition).
class Dataset(BaseModel):
    id: UUID
    project_id: UUID
    filename: str
    sha256: str                 # 64-char lowercase hex
    columns: list[str]
    rowcount: int
    bytes: int
    uploaded_at: datetime

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

-- Phase 3 (alembic 0008): user-uploaded tabular datasets.
CREATE TABLE datasets (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    storage_uri TEXT NOT NULL,           -- file://... in dev, s3://... in prod
    columns JSONB NOT NULL,
    rowcount INT NOT NULL,
    bytes BIGINT NOT NULL,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, sha256)
);
CREATE INDEX ix_datasets_project ON datasets(project_id);
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
| POST | `/projects/{id}/workflow/analysis/approve-code` | **Phase 3.** Approve the Analyst's code (optionally with `override_code`); graph runs the sandbox. |
| POST | `/projects/{id}/workflow/analysis/reject-code` | **Phase 3.** Reject the proposed code; graph re-runs `analyze_propose` with feedback. |
| POST | `/projects/{id}/workflow/analysis/approve-results` | **Phase 3.** Approve the executed results; graph advances to drafting. |
| POST | `/projects/{id}/workflow/analysis/reject-results` | **Phase 3.** Reject the results; graph re-runs `analyze_propose`. |

**Approve / reject payload**
```json
{ "feedback": "Tighten Section 2; remove paragraph about transformers." }
```

The approve payload also accepts two optional, additive fields used by the
Citation Manager (FR-1.5) at the **section** gate:
```json
{ "feedback": "...", "force_unresolved": false, "override_reason": "intentional placeholder" }
```
**Unresolved-citation block.** When approving a drafting section, if the latest
draft cites keys not in the approved pool, the approve is rejected with
`409 { code: "unresolved_citations", keys: [...] }` unless `force_unresolved: true`
is set with an `override_reason`. A forced approve records `forced_unresolved` +
`override_reason` on the `user.approve` audit row so the bypass is auditable.

**Override payload**
```json
{
  "artifact_kind": "section",
  "label": "introduction",
  "content": "## Introduction\n\nThe edited markdown...",
  "mime_type": "text/markdown",
  "citation_corrections": { "smith2020": "lecun2015" },
  "override_reason": "fixed hallucinated key"
}
```
`citation_corrections` and `override_reason` are optional (FR-1.5). When present, each
`{bad: good}` rewrites the exact `[@bad]` marker to `[@good]` in `content` before the
override is applied, and a `user.citation_correction` audit row records the human edit.

**Phase-3 approve-code payload** (`analysis/approve-code`):
```json
{ "feedback": "optional notes", "override_code": "import pandas as pd\\n..." }
```
`override_code` is scanned against the AST denylist (`os`, `subprocess`, `socket`,
`requests`, ...) BEFORE the graph resumes; a denied import returns `422 { code:
"code_static_scan_failed", denied: ["os"] }`. A successful override is recorded as
`analysis.code_overridden` in addition to `analysis.code_approved`.

**Phase-3 reject payloads** (`analysis/reject-code`, `analysis/reject-results`)
require a non-empty `feedback` string — `422 { code: "feedback_required" }` otherwise.
The feedback becomes the LLM's revision instruction on regenerate.

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
| GET | `/projects/{id}/export?format=markdown\|bibtex\|package\|bundle` | Export the manuscript (FR-3.5). See below. |
| GET | `/projects/{id}/drafting/citations?section={section}` | Citation Manager v1 (FR-1.5): resolve a section's cited keys against the approved pool. |

**Export formats (FR-3.5).** Requires a `kind="manuscript"` artifact (Phase 4 done);
otherwise `409 { code: "manuscript_not_ready" }`. Each returns a downloadable file
(`Content-Disposition: attachment`) and writes an `export.generated` audit row.

- `markdown` — the assembled manuscript text (`text/markdown`).
- `bibtex` — references built from the **approved pool only** (FR-2.4 invariant), `application/x-bibtex`.
- `package` — a ZIP (`application/zip`) of separate files under `<slug>/`: `manuscript.md`,
  `references.bib`, `ai-disclosure.md`, `audit-appendix.md` (the BRD §10 disclosure/audit appendix).
- `bundle` — one combined markdown file with all of the above (`text/markdown`).

LaTeX is **not** a valid format; a `latex` value is an ordinary `422` validation error.

### 3.6 Datasets (Phase 3 / FR-2.3)

| Method | Path | Description |
| --- | --- | --- |
| GET | `/projects/{id}/datasets` | List uploaded datasets (newest first). |
| POST | `/projects/{id}/datasets/upload` | Multipart upload (CSV/TSV/JSON/JSONL/Parquet). Returns the populated `Dataset`. |
| DELETE | `/projects/{id}/datasets/{dataset_id}` | Remove a dataset. Locked (`409 phase_locked`) once `run.phase ∈ {analysis, drafting, done}`. |

**Upload constraints**
- Body size cap: `settings.max_dataset_bytes` (default 50 MiB). Larger → `413`.
- Extension whitelist: `.csv .tsv .json .jsonl .parquet`. Unknown → `422`.
- Byte-identical re-upload (same `sha256` + `project_id`): `409 { code: "dataset_duplicate" }`.

**Storage URI scheme.** Dev backend writes `file:///abs/path/...` under
`DATA_DIR/<project_id>/<dataset_id>/<filename>`. Prod adapter (Sprint 6+) swaps for
`s3://bucket/<project_id>/<dataset_id>/<filename>`. The graph's `analyze_execute`
node always reads via `dataset_storage.read_bytes(storage_uri)` so the scheme is
opaque to the agent.

### 3.7 Audit & telemetry

| Method | Path | Description |
| --- | --- | --- |
| GET | `/projects/{id}/audit` | Paginated audit log (newest first). |
| GET | `/projects/{id}/usage` | Token + cost rollup + additive `drafting{}` Phase-4 telemetry block. |

**`/usage` response.** The base `{ tokens_in, tokens_out, cost_usd }` rollup gains an
additive `drafting{}` block (NFR-6 / §9 success metrics), derived from `audit_log`:
```json
{ "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
  "drafting": { "sections_drafted": 7, "regenerations": 3, "overrides": 1,
                "citation_corrections": 2, "avg_section_ms": 8421 } }
```
- `sections_drafted` — `phase_4.section_ready` rows. `regenerations` — `user.reject` rows
  with `payload.phase == "drafting"`. `overrides` — `user.override` rows.
  `citation_corrections` — `user.citation_correction` rows. `avg_section_ms` — mean of
  `draft_ms` on `phase_4.section_ready` rows (`null` if none recorded).

### 3.8 Error format

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
Standard codes: `unauthorized` (401), `forbidden` (403), `not_found` (404), `validation_error` (422), `phase_locked` (409), `token_cap_reached` (402), `provider_error` (502), `manuscript_not_ready` (409, export before Phase 4 done), `unresolved_citations` (409, section approve blocked on hallucinated citation keys), `dataset_duplicate` (409, byte-identical re-upload), `code_static_scan_failed` (422, override or LLM code uses denylisted import), `feedback_required` (422, empty feedback on a Phase-3 reject).

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
| `approval.required` | `{ phase, run_id, summary, section?, gate? }` | Engine has paused awaiting human input. `section` is present only when `phase = "drafting"` (BRD §5.2 FR-2.4). `gate` is present only when `phase = "analysis"` and is one of `"code"` (pre-execution) or `"results"` (post-execution) — SPEC v0.3 / FR-2.3. |
| `usage.tick` | `{ tokens_in, tokens_out, cost_usd }` | Periodic usage rollup (≤ every 5s). |
| `cost.cap_warn` | `{ run_id, spend_usd, cap_usd, warn_pct }` | Project spend crossed `token_cap_warn_pct` of the cap (NFR-5). Advisory — the run continues. |
| `cost.cap_exceeded` | `{ run_id, spend_usd, cap_usd }` | Project spend reached `token_cap_usd` (NFR-5). The run is moved to `error`; the user must raise the cap to continue. |

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
| `analyze_propose` | Analysis | Analyst | `code` artifact + methods narrative (no execution yet — opt-in by dataset presence) |
| `await_code_approval` | Analysis | — | (gate — user reviews code BEFORE sandbox runs, BRD §10 invariant) |
| `analyze_execute` | Analysis | Analyst | Sandbox run; `figure` + `log` artifacts |
| `await_analysis_approval` | Analysis | — | (gate — user reviews results after execution) |
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
  approve & state.datasets non-empty: → analyze_propose
  approve & state.datasets empty:     → draft_section
  reject:                              → synthesize (with feedback)
  override:                            → (analyze_propose | draft_section)

analyze_propose → await_code_approval
  approve:                  → analyze_execute (sandbox run)
  approve + override_code:  → analyze_execute (user-edited code, scanned first)
  reject:                   → analyze_propose (regenerate with feedback)

analyze_execute → await_analysis_approval
  approve:   → draft_section
  reject:    → analyze_propose (regenerate from scratch with feedback)

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

### 6.3 Analyst — Phase 3 (Sandbox Compute)

The Analyst persona ships as **two** invocations per Phase-3 cycle: one
proposes code (gated by `await_code_approval`), one runs it in the
sandbox after approve (gated by `await_analysis_approval`).

```python
class DatasetRef(BaseModel):
    """Schema-only summary the LLM receives — bytes never enter the prompt."""
    id: UUID
    filename: str
    columns: list[str]
    rowcount: int

class AnalystInput(BaseModel):
    project_id: UUID
    task_description: str
    datasets: list[DatasetRef]
    feedback: str | None = None         # populated on regenerate
    prior_code: str | None = None       # populated on regenerate (revise, don't restart)

class StaticScanResult(BaseModel):
    ok: bool                            # False if denied or SyntaxError
    denied: list[str]                   # imports hit by the denylist
    unknown: list[str]                  # imports not in the sandbox image (warn-only)
    error: str | None                   # set when source fails to parse

class AnalystProposal(BaseModel):
    code: Artifact                      # kind="code", produced_by="analyst"
    methods_narrative: str              # quoted by Scribe in §Methodology
    scan: StaticScanResult

class AnalystOutput(BaseModel):
    proposal: AnalystProposal
    usage: AnalystUsage                 # tokens_in/out + cost_usd for cap rollup
```

**Sandbox contract** (`app/services/sandbox.run_in_sandbox`):
- Docker per-call (T2 tier) with `--network=none --read-only
  --cap-drop=ALL --security-opt=no-new-privileges --user=65534:65534
  --pids-limit=64 --memory=512m --memory-swap=512m --cpus=1.0` (defaults
  configurable per call). 60-second wall-clock timeout.
- Output truncation: 64 KiB per stream (`MAX_STDOUT_BYTES` / `MAX_STDERR_BYTES`).
- OOM (Docker exit 137) surfaces as `result.oomed=True`; timeouts as
  `result.timed_out=True`; the function never raises on infrastructure
  failure (sandbox unavailable / docker missing surfaces as a
  `SandboxUnavailableError` only at the run_in_sandbox boundary).

**Static AST scan** (`app/agents/analyst._validate_proposed_code`):
- Hard-rejects imports of `os, sys, subprocess, socket, shutil, tempfile,
  ctypes, requests, urllib*, httpx, aiohttp, pickle, marshal, pty,
  asyncio, multiprocessing, threading, platform, pathlib` (top-level OR
  lazy).
- Warns on imports outside the pre-installed sandbox image
  (`numpy / pandas / matplotlib / scipy / sklearn` + stdlib essentials).
- The same scan applies to `override_code` on `approve-code` (route
  layer) — `422 code_static_scan_failed` on a denied import.

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
5. **Citation guard (FR-1.5).** At the section gate, `approve` is blocked with
   `409 unresolved_citations` when the latest draft cites keys outside the approved
   pool, unless `force_unresolved: true` + `override_reason` is supplied (audited).
   `override` may carry `citation_corrections` to rewrite `[@bad]`→`[@good]` markers,
   recorded as a `user.citation_correction` audit row. This realises BRD risk #1's
   "post-generation validator rejects unknown citation keys" as a hard gate, not just
   a regeneration nudge.

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
