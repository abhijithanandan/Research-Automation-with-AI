# ResearchFlow AI — Complete Project Document

> Single-source reference for the platform: what it is, why it exists, how every
> moving part fits together, and where it sits against the BRD roadmap. Pulls
> together what's spread across `BRD.md`, `SPEC.md`, `ARCHITECTURE.md`,
> `hardening-closure.md`, the phase compliance reports, and the
> `docs/brd-verification-and-phase3-plan.md` ledger.

**Status snapshot (2026-05-31, `main @ 86020e3`):**
Phases 1, 2, 3, and 4 are all implemented and merged. 373 backend tests passing.
SPEC at v0.3. Audit closure delivered (0/0/0 bandit, all 19 findings closed).

---

## Table of contents

1. [What ResearchFlow AI is](#1-what-researchflow-ai-is)
2. [Mission, principles, non-goals](#2-mission-principles-non-goals)
3. [Roadmap status — phase by phase](#3-roadmap-status--phase-by-phase)
4. [The four agents](#4-the-four-agents)
5. [The HITL workflow contract](#5-the-hitl-workflow-contract)
6. [System architecture](#6-system-architecture)
7. [Backend layer (FastAPI + LangGraph + Postgres + Chroma)](#7-backend-layer)
8. [Frontend layer (Next.js + React + Tailwind v4)](#8-frontend-layer)
9. [Repository layout](#9-repository-layout)
10. [Domain data model](#10-domain-data-model)
11. [REST API surface](#11-rest-api-surface)
12. [WebSocket event stream](#12-websocket-event-stream)
13. [Security posture](#13-security-posture)
14. [Operability — observability, cost cap, reproducibility](#14-operability)
15. [Testing strategy](#15-testing-strategy)
16. [CI/CD gates and branch protection](#16-cicd-gates-and-branch-protection)
17. [Local development](#17-local-development)
18. [Audit history (waves 1-3 + CodeRabbit rounds 1-4)](#18-audit-history)
19. [Known limitations + accepted risks](#19-known-limitations--accepted-risks)
20. [Where to go next](#20-where-to-go-next)

---

## 1. What ResearchFlow AI is

ResearchFlow AI is a hybrid multi-agent system that helps a researcher go from
a single seed query (e.g. "autonomous coding agents") to a citation-grounded
literature-review manuscript, with every consequential decision gated by an
explicit human approval. It runs as a local Next.js client talking to a cloud
FastAPI backend; the backend owns LLM inference, retrieval, sandboxed compute,
and audit-trail persistence.

The system orchestrates **four agentic personas** — Librarian, Critic, Analyst,
Scribe — through a four-phase workflow. Phase boundaries are hard gates: the
state machine cannot advance without an explicit `approve` event from the user.
Every model call, paper, approval, and override lands in an append-only audit
log; on export, an AI-disclosure appendix and the full audit log are bundled
with the manuscript so the resulting document is reproducible and accountable.

---

## 2. Mission, principles, non-goals

### Mission

Cut the time a researcher needs to produce a defensible draft literature review
from days to under an hour, without giving up the integrity of citation-grounded
academic prose.

### Principles (from BRD §1)

- **Human in the loop everywhere.** The agents are co-pilots. Every phase ends
  at an interrupt that requires explicit `approve` / `reject` / `override`.
- **Cite only from the approved pool.** The Scribe cannot cite a paper the user
  hasn't accepted into the working set; a post-generation validator enforces it
  and the Citation Manager UI surfaces every key for inspection.
- **Audit-first.** Every LLM call, every approval, every override is a row in
  `audit_log` with `actor`, `action`, `model`, `tokens_in/out`, `cost_usd`,
  `payload`, and an ISO timestamp.
- **Cost is a first-class constraint.** Each project has a token-cap (default
  $5 USD); the workflow halts when spend crosses the cap.
- **Strict provenance separation.** Artifacts carry a `produced_by` field
  (`librarian | critic | analyst | scribe | human`) so audit reviewers can tell
  AI-written from human-edited spans at a glance.

### Non-goals

- Mobile or tablet layouts. Desktop only.
- Light mode. The interface is pitch-black + emerald monochrome.
- Multi-tenancy at the application layer. Per-user scoping happens via Firebase
  UID + DB ownership checks; org-level RBAC is out of scope until v1+.
- Replacing the curator's judgment. Bot-detection bypass, paywall circumvention,
  and silent publication of AI-only output are explicit non-goals.

---

## 3. Roadmap status — phase by phase

The BRD lays out four phases. As of `main @ 86020e3`, **all four are merged**.

| Phase | BRD ref | Persona | Status | Where it lives |
| --- | --- | --- | --- | --- |
| **Phase 1 — Query & Discovery** | §4.1, §5.2 FR-2.1 | Librarian | ✅ shipped | `app/agents/librarian.py`, `app/services/discovery.py` (5 source adapters: Semantic Scholar, arXiv, Crossref, CORE, Europe PMC), `app/services/discovery_router.py` |
| **Phase 2 — Synthesis** | §4.1, §5.2 FR-2.2 | Critic | ✅ shipped | `app/agents/critic.py`, `app/services/fulltext_fetcher.py` (Unpaywall + ChromaDB ingestion), `app/services/vector_store.py` |
| **Phase 3 — Data analysis** | §4.1, §5.2 FR-2.3 | Analyst | ✅ shipped (v0.2) | `app/agents/analyst.py`, `app/services/sandbox.py`, `backend/sandbox/Dockerfile`, `app/services/dataset_storage.py` |
| **Phase 4 — Drafting** | §4.1, §5.2 FR-2.4 | Scribe | ✅ shipped | `app/agents/scribe.py`, `node_draft_section` / `node_assemble` in `app/graph/workflow.py` |

A Phase-3 commit history lives on `main`:
- `edb7b0f` Sprint 1 — dataset upload pipeline (1,383 lines)
- `51416d1` Sprint 2 — Analyst agent: LLM proposal + AST denylist scan
- `0eea9a9` Sprint 3 — Docker T2 sandbox + image + integration tests
- `6aa07a5` Sprint 4 — graph wiring: two HITL gates + approve-code/approve-results routes
- `d3aeba9` Sprint 5 — frontend `AnalysisReview` component + analysis API client
- `86020e3` Sprint 6 — SPEC v0.3, README status, closure record (PHASE3_IMPLEMENTATION_PLAN.md)

**Deferred to v0.3 or later** (BRD §12):
- FR-1.3 — local browser automation via Playwright (scoped to v0.3).
- LaTeX manuscript output (v0.2 line item but independent of the Analyst).
- Multi-project dashboard.
- gVisor / Firecracker sandbox tier (T3, slated for v1.0 production hardening).

---

## 4. The four agents

Each agent is a thin `Agent` subclass (`backend/app/agents/base.py`) wrapping
an LLM call + structured input/output. Prompt templates live in source so the
NFR-4 reproducibility guarantee holds.

### Librarian (Phase 1)

Fans out the seed query across five academic sources concurrently, expands
synonyms via the LLM, deduplicates by DOI then fuzzy title match
(`thefuzz.token_set_ratio` at threshold 86), and ranks the surviving pool by a
citation-velocity heuristic (`citations / paper_age_years`, log-scaled).
Returns a list of candidate `Paper` rows with `approved=False`.

- **Sources**: Semantic Scholar, arXiv, Crossref, CORE, Europe PMC.
- **Resilience**: per-source retry budget (tenacity, 3 attempts with exponential
  backoff + Retry-After header honoring); on exhaustion raises
  `SourceUnavailableError` which the `discovery_router` counts toward
  fail-fast (`consecutive_failures >= 2` short-circuits the rest of that
  source's queries).
- **Prompt safety**: untrusted strings (abstracts, prior section content,
  reviewer feedback) are XML-wrapped via `app/agents/_prompt_safety.py`
  (`safe_tag`, `xml_escape`, `SYSTEM_ANCHOR`) so a poisoned upstream abstract
  cannot redirect the agent.

### Critic (Phase 2)

Reads the user-approved pool. Optionally enriches each paper's PDF URL via
Unpaywall, downloads the OA full-text via `app/services/fulltext_fetcher.py`,
chunks the text (paragraph-based with hard `_MAX_CHUNK_CHARS=1800` cap), and
embeds chunks into the project's ChromaDB namespace. Then performs a single
batched LLM call to extract five attributes per paper
(problem / method / dataset / key_findings / limitations), produces a
JSON-encoded comparison matrix `Artifact`, and writes a 3-6 paragraph narrative
synthesis grouped by methodological cluster as a separate `Artifact`.

- **Fulltext fetch** runs concurrently under `Semaphore(5)`, emits per-paper
  `fulltext_progress` WS events, and uses a strict urlparse + netloc + path
  allow-list (no substring matching that could let `evil.com/?fake=arxiv.org`
  through). Redirects are followed manually (`follow_redirects=False` on the
  httpx client) and each `Location` is re-validated against the allow-list.
- **Cost mode**: batched extraction (one LLM call for N papers in an envelope
  shape) so we stay inside the Gemini free-tier daily budget on small pools.

### Analyst (Phase 3, v0.2)

Generates Python code from a user-supplied task description + dataset schema,
shows the code to the user for approval (gate 1), runs the approved code in a
hardened Docker sandbox, then shows the resulting figures + log + a one-
paragraph methods narrative to the user for approval (gate 2).

- **Pre-execution defense**: an AST scan inside `analyze_propose` rejects code
  that imports any of `os`, `subprocess`, `socket`, `ctypes`, `requests`,
  `urllib`. A reviewer can override the block, but the override lands loud
  in the audit log.
- **Sandbox tier T2** (`app/services/sandbox.py` + `backend/sandbox/Dockerfile`):
  per-call `python:3.11-alpine` containers with `--network=none --read-only
  --cap-drop=ALL --user 65534:65534 --memory=512m --memory-swap=512m
  --cpus=1.0 --pids-limit=64 --security-opt=no-new-privileges` and a seccomp
  profile that drops `unshare`, `clone(CLONE_NEWUSER)`, `ptrace`. 60-second
  wall-clock kill, 64 KiB stdout/stderr cap, EXIF stripping on figures before
  serving back.

### Scribe (Phase 4)

Per-section drafting in canonical order (abstract → introduction →
related_work → methodology → results → discussion → conclusion). Each draft
goes through a HITL gate; `node_assemble` concatenates the approved drafts
into a single `kind="manuscript"` Artifact in canonical order regardless of
the order they were drafted.

- **Cite-only-from-pool**: a post-generation validator extracts every `[@key]`
  marker, verifies each key is in `approved_pool`, and on first failure forces
  one regeneration with the invalid keys passed back as feedback. On second
  failure the invalid keys are surfaced as `INVALID:` chips in the Citation
  Manager panel and the section still goes to the gate (the reviewer
  decides what to do).
- **Telemetry**: each section ships its `draft_ms` into the
  `phase_4.section_ready` audit row so `/usage` can roll up
  `avg_section_ms` along with `sections_drafted`, `regenerations`,
  `overrides`, and `citation_corrections`.

---

## 5. The HITL workflow contract

LangGraph state machine, declared in `app/graph/workflow.py`:

```
discover ─→ await_pool_approval ──[approve]──→ synthesize
                                ──[reject]──→ discover (with feedback)

synthesize ─→ await_synthesis_approval
              ──[approve + has_dataset]──→ analyze_propose
              ──[approve + no_dataset]───→ draft_section (skip Phase 3)
              ──[reject]──→ synthesize (with feedback)
              ──[override]──→ draft_section (with human matrix/summary)

analyze_propose ─→ await_code_approval
                   ──[approve]──→ analyze_execute
                   ──[reject]──→ analyze_propose (regen with feedback)

analyze_execute ─→ await_analysis_approval
                   ──[approve]──→ draft_section
                   ──[reject]──→ analyze_propose
                   ──[override]──→ draft_section (with human results)

draft_section ─→ await_section_approval
                 ──[approve + sections_remaining]──→ draft_section (next)
                 ──[approve + done]──→ assemble
                 ──[reject]──→ draft_section (current, with feedback)
                 ──[override]──→ await_section_approval (next)

assemble ─→ __end__
```

### Gate invariants (SPEC §5.3)

1. The graph **persists a checkpoint before** emitting `interrupt()` (LangGraph
   does this internally via the Postgres `AsyncPostgresSaver` checkpointer).
2. The REST `/workflow/approve|reject|override` endpoint looks up the active
   `WorkflowRun`, verifies it is `awaiting_approval`, and dispatches a
   `Command(resume=...)`. Any other state returns `409 phase_locked`.
3. Every approval action writes an `audit_log` entry with `actor="user"` and
   the user UID in `payload`.
4. Frontend may not advance its local view until a `state.changed` WS event
   confirms the transition. No optimistic UI for gate transitions.
5. **Defensive default**: any resume value that isn't the literal `"approve"`
   is treated as `reject` (audit finding #6 mitigation, parametrised tests in
   `test_workflow_contract.py`).
6. **Code-before-execute (Phase 3)**: BRD §4.1 invariant. The Analyst's
   `analyze_propose` node produces code; the user reviews it at
   `await_code_approval` before `analyze_execute` ever runs.

### Citation guard at the section gate (FR-1.5)

When approving a drafting section, the route checks for unresolved citation
keys (cited but not in the approved pool). If any are found and the caller
hasn't passed `force_unresolved=true` + a non-empty `override_reason`, the
approve returns `409 unresolved_citations` with the offending keys.
Force-approve writes `forced_unresolved` + `override_reason` to the audit log;
`ApprovePayload` has a Pydantic `model_validator` that enforces the reason at
the schema level (a curl call cannot bypass the frontend's required-field).

---

## 6. System architecture

```
+----------------------------------+        +-----------------------------------+
|         LOCAL CLIENT             |        |        REMOTE ENGINE              |
|  (Next.js 14, React 18, TS)      |        |  (FastAPI + LangGraph + asyncio)  |
|                                  |        |                                   |
|  - App Router pages              |        |  - REST routes (14 endpoints)     |
|  - WebSocket client (auto-       |◀──WSS──▶  - WS endpoint (auth-handshake)  |
|    reconnect with backoff)       |        |  - LangGraph workflow runtime     |
|  - Typed REST client (api.ts)    |◀──HTTPS▶  - 4 agents (Librarian, Critic,  |
|  - Review panels (Approval /     |        |    Analyst, Scribe)               |
|    Synthesis / Section /         |        |  - 5 discovery adapters           |
|    Analysis)                     |        |  - Unpaywall + fulltext fetcher   |
|  - Markdown rendering            |        |  - Sandbox service (Docker T2)    |
|  - Diff util                     |        |                                   |
+----------------------------------+        +-----------------------------------+
                                                          │
                          ┌───────────────────────────────┼───────────────────────────────┐
                          │                               │                               │
                          ▼                               ▼                               ▼
                  +---------------+              +-----------------+            +------------------+
                  |  Postgres 16  |              |   ChromaDB      |            |  Firebase Auth   |
                  | (workflow_    |              | (per-project    |            | (Google OAuth)   |
                  |  runs,        |              |  vector         |            |                  |
                  |  artifacts,   |              |  namespaces,    |            |                  |
                  |  audit_log,   |              |  paper          |            |                  |
                  |  datasets,    |              |  abstracts +    |            |                  |
                  |  papers,      |              |  fulltext       |            |                  |
                  |  projects,    |              |  chunks)        |            |                  |
                  |  users)       |              +-----------------+            +------------------+
                  +---------------+
                          │
                          │
                          ▼
                  +-----------------+
                  | LangGraph       |
                  | checkpointer    |
                  | (AsyncPostgres- |
                  | Saver, same DB) |
                  +-----------------+
```

Five top-level moving parts:

| Component | Tech | Purpose |
| --- | --- | --- |
| **Local client** | Next.js 14 (App Router), React 18, TypeScript, Tailwind CSS v4 | UI, review panels, WebSocket subscription, REST calls |
| **Remote engine** | FastAPI + Python 3.11 + asyncio | LLM orchestration, REST + WS API, business logic |
| **Workflow runtime** | LangGraph 1.2 with `AsyncPostgresSaver` | State machine + HITL `interrupt()` gates |
| **Vector store** | ChromaDB 1.5 | RAG context for Critic and Scribe; per-project namespacing |
| **Relational DB** | PostgreSQL 16 | Project metadata, workflow state, audit log, datasets, papers |

Optional auxiliaries:
- **LLM providers**: Gemini 2.5 / 3.5 Flash (default), Anthropic Claude 4 (drop-in via `LLM_PROVIDER=anthropic`).
- **Firebase Auth**: Google OAuth, ID-token verification on every HTTP and WS handshake.
- **Docker daemon**: required for Phase 3 sandbox.

---

## 7. Backend layer

### Folder layout

```
backend/
├── app/
│   ├── agents/           # 4 personas + base + prompt safety
│   ├── api/
│   │   ├── deps.py       # auth dependency, current_user resolver
│   │   ├── middleware.py # body-size cap (1 MiB JSON / 50 MiB upload)
│   │   ├── rate_limit.py # per-actor sliding-window rate limiter
│   │   └── routes/       # 7 router files
│   ├── db/session.py     # AsyncSession factory + flush_for_background_dispatch
│   ├── graph/
│   │   ├── state.py      # GraphState TypedDict
│   │   └── workflow.py   # 11 nodes + 5 conditional edges
│   ├── models/
│   │   ├── db.py         # SQLAlchemy 2.0 declarative rows
│   │   └── schemas.py    # Pydantic wire types
│   ├── services/         # business logic + external integrations
│   │   ├── auth.py
│   │   ├── citations.py
│   │   ├── dataset_storage.py
│   │   ├── discovery.py
│   │   ├── discovery_router.py
│   │   ├── export.py
│   │   ├── fulltext_fetcher.py
│   │   ├── llm.py        # LLM gateway abstraction (Gemini + Anthropic)
│   │   ├── sandbox.py    # Phase-3 Docker T2 sandbox
│   │   ├── unpaywall.py
│   │   ├── vector_store.py
│   │   └── workflow.py   # graph dispatch + WS bus + audit writer
│   ├── utils/logging.py  # structlog JSON config
│   ├── config.py         # pydantic-settings — all env-var reads
│   └── main.py           # FastAPI app + lifespan
├── alembic/versions/     # 8 migrations
├── sandbox/Dockerfile    # Phase-3 sandbox image (python:3.11-alpine)
├── scripts/preflight.py
├── tests/                # 373 pytest tests across 31 files
├── pyproject.toml
├── requirements-lock.txt
└── run_ci_local.sh
```

### Database migrations

8 alembic revisions establish the schema:

1. `0001` — initial schema (users, projects, workflow_runs, papers, artifacts, audit_log, ws_events_outbox).
2. `0002` — partial unique on `(project_id, citation_key)` for papers idempotency.
3. `0003` — `ON DELETE CASCADE` from projects to owner.
4. `0004` — partial unique on `(project_id) WHERE state IN ('running', 'awaiting_approval')` so at most one active run per project.
5. `0005` — partial unique on `(project_id) WHERE kind='manuscript'` so a project has at most one manuscript.
6. `0006` — partial unique on `(workflow_run_id) WHERE action='phase_1.approved_pool'` (anti-double-fire).
7. `0007` — CHECK constraint on `workflow_runs.state IN (...)` matching `VALID_RUN_STATES`.
8. `0008` — `datasets` table for Phase-3 dataset uploads.

### Process model

- **Dev**: Uvicorn with `--reload` inside the backend Docker container.
- **Test**: `pytest-asyncio` in auto-mode; AsyncPostgresSaver swapped for `MemorySaver` in some unit tests; full integration tests use the real Postgres compose service.
- **Background tasks**: graph dispatch happens in an `asyncio.create_task`
  spawned from the request handler so the HTTP response returns
  immediately (`flush_for_background_dispatch` commits the session before
  spawn). Tasks live in `_background_tasks: set[asyncio.Task]` and are
  GC-pinned.

---

## 8. Frontend layer

### Folder layout

```
frontend/
├── app/
│   ├── layout.tsx        # root layout
│   ├── globals.css       # Tailwind v4 @theme block (OKLCH palette)
│   └── page.tsx          # single-page state machine — 10 view states
├── components/workflow/
│   ├── AnalysisReview.tsx        # Phase-3 code review + figures + log
│   ├── ApprovalPanel.tsx         # Phase-1 paper selector
│   ├── DatasetUploader.tsx       # Phase-3 dataset upload + listing
│   ├── DraftingTelemetryChips.tsx
│   ├── ExportPanel.tsx           # 4-format export picker
│   ├── MatrixModal.tsx           # fullscreen matrix portal
│   ├── PhaseTracker.tsx          # vertical stepper
│   ├── SectionReview.tsx         # Phase-4 per-section gate
│   ├── SynthesisReview.tsx       # Phase-2 matrix + narrative
│   ├── Markdown.tsx              # react-markdown wrapper
│   └── diffLines.ts              # LCS diff for override editor
├── lib/
│   ├── api.ts                    # typed REST client (every endpoint)
│   ├── ws.ts                     # ManagedSocket with reconnect + backoff
│   ├── types.ts                  # TS mirrors of Pydantic models
│   └── utils.ts                  # cn() helper
├── package.json
├── postcss.config.js             # Tailwind v4 plugin
└── tsconfig.json
```

### Design system

Pitch-black + emerald monochrome (no amber/violet/blue chrome). OKLCH-based
palette declared in `globals.css` under Tailwind v4's `@theme` block:

```css
--color-background:    oklch(0% 0 0);            /* pure black */
--color-foreground:    oklch(95% 0 0);           /* near-white text */
--color-border:        oklch(20% 0 0);           /* single hairline */
--color-primary:       oklch(72% 0.20 155);      /* emerald */
--color-primary-dim:   oklch(45% 0.12 155);      /* emerald-700 review-gate */
--color-destructive:   oklch(60% 0.22 27);       /* red, rare */
```

Strict rule: no Tailwind arbitrary-color classes (`bg-[#xxxxxx]`).
`scripts/check_forbidden_patterns.sh` greps for them and fails CI on match.

### State machine

`page.tsx` carries a single `view` state with 10 values: `idle`, `creating`,
`running`, `awaiting`, `synthesis`, `drafting`, `busy`, `done`, `error`,
`analysis` (Phase 3). The WS event handler is the only place that transitions
the view; REST calls flip to `busy` and let the WS land the final state.

Reviews are the heart of the UI: every Phase-N review has a tabbed panel
(`Preview` / `Source` / `Citations` / `Diff vs previous`) above a Review-gate
section with `Approve` / `Reject (regenerate)` / `Edit & override` buttons.
The override editor uses the `diffLines` LCS util to render +/- per line.

---

## 9. Repository layout

Top-level tree:

```
.
├── .agents/skills/        # 30 installed skills (claude-api, security-audit, etc.)
├── .audit/                # radon waivers
├── .github/workflows/     # backend-ci.yml + frontend-ci.yml
├── .husky/                # pre-commit hook (set -e + secret scan + lint-staged)
├── alembic/, backend/     # backend monolith
├── frontend/              # Next.js client
├── docs/                  # implementation plans + runbook + audit reports
├── reports/               # bandit.json, radon-cc.txt, eslint.json, npm-audit.json, findings-matrix.md, remediation-backlog.md
├── scripts/               # apply_branch_protection.sh, check_radon_budget.sh, check_secrets.sh, check_forbidden_patterns.sh
├── ARCHITECTURE.md        # system architecture (separate doc)
├── BRD.md                 # business requirements (the source-of-truth for what to build)
├── SPEC.md                # technical contract (REST + WS + data model)
├── README.md
├── RUNBOOK.md             # how to start/stop the stack via Docker Compose
├── PROJECT.md             # THIS FILE
├── baseline.md            # env + dep snapshot at audit/2026-05-31
├── hardening-closure.md   # full audit-wave 1/2/3 ledger
├── phase_1_compliance_report.md
├── phase_4_brd_compliance_report.md
├── docker-compose.yml     # postgres + chroma + backend + frontend
└── package.json           # root-level (commitlint, husky)
```

---

## 10. Domain data model

Defined in `backend/app/models/schemas.py` (Pydantic) and `models/db.py`
(SQLAlchemy). TypeScript mirrors live in `frontend/lib/types.ts`.

```python
class Phase(StrEnum):
    DISCOVERY = "discovery"
    SYNTHESIS = "synthesis"
    ANALYSIS  = "analysis"
    DRAFTING  = "drafting"
    DONE      = "done"

VALID_RUN_STATES = frozenset({"running", "awaiting_approval", "approved", "rejected", "error"})

class User(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str | None
    created_at: datetime

class Project(BaseModel):
    id: UUID
    owner_id: UUID
    title: str
    seed_query: str
    output_format: Literal["markdown", "latex"] = "markdown"
    token_cap_usd: float = 5.0
    status: Literal["draft", "active", "completed", "archived"] = "draft"
    current_phase: Phase = Phase.DISCOVERY
    created_at: datetime
    updated_at: datetime

class WorkflowRun(BaseModel):
    id: UUID
    project_id: UUID
    phase: Phase
    state: Literal["running", "awaiting_approval", "approved", "rejected", "error"]
    checkpoint_id: str
    started_at: datetime
    awaiting_since: datetime | None
    last_event_at: datetime

class Paper(BaseModel):
    id: UUID
    project_id: UUID | None
    source: Literal["semantic_scholar", "arxiv", "crossref", "core", "europe_pmc", "upload"]
    external_id: str
    title: str
    authors: list[str]
    year: int | None
    abstract: str | None
    pdf_url: HttpUrl | None
    citation_key: str
    citation_count: int | None
    approved: bool = False
    added_at: datetime

ArtifactKind = Literal["matrix", "summary", "section", "manuscript", "figure", "code", "log"]
ProducedBy   = Literal["librarian", "critic", "analyst", "scribe", "human"]

class Artifact(BaseModel):
    id: UUID
    project_id: UUID
    kind: ArtifactKind
    label: str
    content: str
    mime_type: str
    produced_by: ProducedBy
    parent_id: UUID | None
    created_at: datetime

class Dataset(BaseModel):                  # NEW in v0.2 / Phase 3
    id: UUID
    project_id: UUID
    filename: str
    sha256: str
    columns: list[str]
    rowcount: int
    bytes: int
    uploaded_at: datetime

class AuditLogEntry(BaseModel):
    id: UUID
    project_id: UUID
    workflow_run_id: UUID | None
    actor: Literal["system", "user", "librarian", "critic", "analyst", "scribe"]
    action: str                            # e.g. "user.approve", "phase_4.section_ready", "analysis.code_proposed"
    payload: dict[str, object]
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    created_at: datetime
```

---

## 11. REST API surface

All routes mounted under `/api/v1`. Auth via `Authorization: Bearer <firebase-id-token>` on every endpoint.

### Health / metadata
- `GET /health` — liveness + version.
- `GET /meta/providers` — which LLM/vector/auth providers are configured.

### Projects
- `POST /projects` — create. Rate-limit: 30/min/user.
- `GET /projects` — list owned.
- `GET /projects/{id}` — single project.
- `PATCH /projects/{id}` — update title/seed_query/token_cap_usd.
- `DELETE /projects/{id}` — soft-archive.

### Workflow control
- `POST /projects/{id}/workflow/start` — begin Phase 1. Rate-limit: 10/min/user.
- `GET /projects/{id}/workflow` — current `WorkflowRun`.
- `POST /projects/{id}/workflow/approve` — advance current gate. Body: `{feedback?, force_unresolved?, override_reason?}`. Rate-limit: 30/min/user.
- `POST /projects/{id}/workflow/reject` — re-run current phase with feedback. Rate-limit: 30/min/user.
- `POST /projects/{id}/workflow/override` — submit human-edited artifact. Body includes `citation_corrections` map + optional `override_reason`. Rate-limit: 20/min/user.
- `POST /projects/{id}/workflow/analysis/approve-code` — Phase-3 code-review gate.
- `POST /projects/{id}/workflow/analysis/approve-results` — Phase-3 results-review gate.

### Papers
- `GET /projects/{id}/papers`
- `POST /projects/{id}/papers/upload` — multipart PDF upload (501 in v0.1; will land alongside v0.3 browser-use work).
- `PATCH /projects/{id}/papers/{paper_id}` — toggle approved or fix metadata. Rate-limit: 60/min/user.
- `DELETE /projects/{id}/papers/{paper_id}`. Rate-limit: 60/min/user.

### Datasets (Phase 3)
- `GET /projects/{id}/datasets` — list uploaded datasets.
- `POST /projects/{id}/datasets` — multipart upload of CSV / JSON / Parquet. 50 MiB cap.
- `DELETE /projects/{id}/datasets/{dataset_id}` — only allowed before Phase 3 starts.

### Artifacts + audit
- `GET /projects/{id}/artifacts?kind=...` — list.
- `GET /projects/{id}/artifacts/{artifact_id}` — single.
- `GET /projects/{id}/audit` — paginated audit log.
- `GET /projects/{id}/usage` — token/cost rollup + Phase-4 `drafting{}` telemetry block.

### Export pack (FR-3.5)
- `GET /projects/{id}/export?format=markdown` — assembled manuscript.
- `GET /projects/{id}/export?format=bibtex` — approved-pool references only.
- `GET /projects/{id}/export?format=package` — ZIP: manuscript + references + AI disclosure + audit appendix.
- `GET /projects/{id}/export?format=bundle` — single combined markdown.
- Requires manuscript present; otherwise `409 manuscript_not_ready`.

### Drafting citations (FR-1.5)
- `GET /projects/{id}/drafting/citations?section=...` — `SectionCitationPanel` with cited / unresolved / resolved entries (title + authors + year + URL).

### Error envelope (SPEC §3.7)

```json
{ "error": { "code": "phase_locked", "message": "...", "trace_id": "..." } }
```

Codes: `unauthorized`, `forbidden`, `not_found`, `validation_error`,
`phase_locked`, `token_cap_reached`, `provider_error`, `manuscript_not_ready`,
`unresolved_citations`, `invalid_citation_correction`.

---

## 12. WebSocket event stream

Endpoint: `ws://host/api/v1/projects/{project_id}/events`.

Auth handshake: first client message is `{"type": "auth", "token": "<firebase-id-token>"}`. Server replies `{"type": "auth.ok"}` or closes with code `4401` (auth failure) / `4403` (not owner) / `4429` (rate-limit, 20 connections / 10s / IP).

Server → client event types (discriminated union, defined in
`frontend/lib/ws.ts`):

| Event | Emitted when | Payload (selected fields) |
| --- | --- | --- |
| `auth.ok` | Handshake succeeded | `ts` |
| `state.changed` | Run advanced phase or state | `phase`, `state`, `run_id` |
| `agent.started` | An agent's node entered | `agent`, `run_id` |
| `agent.token` | Token-stream chunk | `agent`, `delta` |
| `agent.completed` | An agent finished | `agent`, `artifact_ids` |
| `agent.error` | An agent failed | `agent`, `run_id`, `error_code`, `error` (sanitized, generic message) |
| `approval.required` | A gate is waiting | `phase`, `run_id`, `summary`, `section?` |
| `fulltext_progress` | Per-paper fulltext ingest tick | `done`, `total` |
| `analysis.code_ready` | Phase 3 — code generated, awaiting approval | `run_id`, `code_artifact_id` |
| `analysis.results_ready` | Phase 3 — execution finished, awaiting approval | `run_id`, `figure_ids`, `log_id` |
| `usage.tick` | Cost-rollup ping | `tokens_in`, `tokens_out`, `cost_usd` |
| `cost.cap_warn` | Spend ≥ warn_pct of cap | `spend_usd`, `cap_usd`, `warn_pct` |
| `cost.cap_exceeded` | Spend ≥ cap; run halted | `spend_usd`, `cap_usd` |
| `pong` | Reply to client ping | `ts` |

`_REPLAY_TYPES` cache: `approval.required`, `state.changed`, `agent.error`,
`cost.cap_exceeded` are cached by `_last_event` so late subscribers get
replayed the most recent significant event on connect.

### Reconnect contract

`frontend/lib/ws.ts` (`ManagedSocket`) reconnects with exponential backoff +
jitter on close codes outside the no-reconnect set (`4401`, `4403` = give up;
`4429` = retry with longer delay). `_RECONNECT_MAX_ATTEMPTS = 10`.

---

## 13. Security posture

### Authentication

- Firebase Admin SDK verifies the ID token on every HTTP and WS handshake.
  The same `resolve_or_create_user` / `resolve_user_id` helpers run on both
  transports so identity cannot drift.
- `DEV_AUTH_BYPASS=true` is permitted only when `APP_ENV=development`; the
  lifespan hook refuses to boot otherwise.

### Authorization

- Every project-scoped route calls `_assert_owned()` / `_assert_project_owned()`
  before any work. Owner mismatch returns `403 forbidden`.
- WS auth also re-verifies project ownership against the DB before sending any
  event.

### Input validation

- Pydantic `BaseModel` on every payload; literal-typed `artifact_kind`,
  `mime_type`, `output_format` so a crafted client can't write garbage strings
  into the DB.
- Body-size cap: 1 MiB JSON globally, 50 MiB on upload endpoints
  (`BodySizeLimitMiddleware`).
- Rate limits: per-actor sliding-window via `app/api/rate_limit.py`. Throttled
  endpoints: `project.create`, `workflow.start`, `workflow.approve`,
  `workflow.reject`, `workflow.override`, `paper.update`, `paper.delete`, WS
  handshake.

### Prompt injection (OWASP LLM01)

- All untrusted strings (abstracts, prior sections, reviewer feedback,
  Analyst task descriptions) are XML-wrapped via `safe_tag` / `xml_escape`
  before landing in any LLM prompt. `SYSTEM_ANCHOR` lands at the END of every
  prompt template re-affirming that data inside `<paper>` / `<abstract>` /
  `<reviewer_feedback>` / `<focus>` is untrusted.

### XML parsing

- arXiv Atom feed is parsed via `defusedxml.ElementTree` (rejects DTD,
  external entities, billion-laughs). bandit B314 stops firing on this branch.

### URL allow-list + redirect control

- Fulltext fetcher uses strict `urlparse(...).netloc` + path-prefix matching
  against `_OA_HOSTS_ALLOWLIST` (no substring matching). `httpx.AsyncClient`
  is configured with `follow_redirects=False`; the download function walks
  the redirect chain manually (5-hop cap) and re-validates every `Location`.

### Citation-correction validation (FR-1.5)

- `citation_corrections` map in the override payload is validated against the
  approved pool before being applied: replacement keys must be pool members or
  the route returns `422 invalid_citation_correction`. The post-application
  audit row records the rewrite in a single regex pass (no cascading replace).

### Sandbox isolation (Phase 3)

- Docker T2 per-call: `python:3.11-alpine` containers with
  `--network=none --read-only --cap-drop=ALL --user 65534:65534 --memory=512m
  --memory-swap=512m --cpus=1.0 --pids-limit=64 --security-opt=no-new-privileges`
  + seccomp denylist (`unshare`, `clone(CLONE_NEWUSER)`, `ptrace`).
- AST scan rejects code that imports `os` / `subprocess` / `socket` /
  `ctypes` / `requests` / `urllib`. Override path is loud in audit log.
- 60 s wall-clock kill. stdout/stderr capped at 64 KiB.
- EXIF stripping on figures before serving back.

### Secret management

- `.env` git-ignored. `scripts/check_secrets.sh` runs at pre-commit (`.husky/pre-commit`)
  with `set -e` so a hit aborts the commit.
- Forbidden-pattern grep in `scripts/check_forbidden_patterns.sh` blocks raw
  SQL interpolation, `bg-[#hex]` Tailwind classes, and `subprocess` imports
  outside the sandbox module.

### Audit-trail integrity

- `force_unresolved=true` on approve requires a non-empty `override_reason`
  at the Pydantic schema level. A curl call cannot bypass the frontend's
  required-field.
- Every consequential action writes an `audit_log` row with `actor`, `action`,
  `payload`, `model`, `tokens_in/out`, `cost_usd`, `created_at`.

---

## 14. Operability

### Observability (NFR-6)

- structlog JSON output everywhere. Standard fields: `event`, `error_type`,
  `project_id`, `run_id`, `actor`, `result`, `reason_code`.
- Trace-ID end-to-end correlation is partial (logs carry `project_id` and
  `run_id` but no explicit `trace_id`); full UI → API → LLM correlation is a
  carry-forward.

### Cost cap (NFR-5)

- `_enforce_cost_cap` in `services/workflow.py` sums `audit_log.cost_usd` per
  project and compares to `projects.token_cap_usd`. Spend ≥ `warn_pct * cap`
  emits `cost.cap_warn`; spend ≥ cap halts the run (`state=error` + audit row).
- Default cap: $5 USD per project. Default warn threshold: 80%.

### Reproducibility (NFR-4)

- Every prompt template lives in source (`_PROMPT_TEMPLATE`,
  `_EXTRACTION_PROMPT_TEMPLATE`, `_BATCH_EXTRACTION_PROMPT_TEMPLATE`,
  `_SYNTHESIS_PROMPT_TEMPLATE`, etc.). Diffable in git.
- `requirements-lock.txt` pins the full third-party closure. CI installs
  from the lock first, then `pip install -e ".[dev]" --no-deps`.
- `scripts/preflight.py` asserts the compat-trio (pydantic / langchain-core /
  langgraph) versions match the pyproject pins at boot.

### Resilience

- Discovery adapters: 3-attempt tenacity retry with exponential backoff;
  `Retry-After` header honored on 429 (capped at 60s); retry exhaustion raises
  `SourceUnavailableError` which the router counts as failure.
- Fulltext fetch: `Semaphore(5)` concurrency, per-paper try/except/finally so
  one bad PDF doesn't sink progress accounting.
- Workflow runtime: orphan-cleanup at startup moves `state=running` runs to
  `state=error` (LangGraph thread died with the previous process). Idempotent
  first-login under concurrent requests via `IntegrityError` catch + re-SELECT.

---

## 15. Testing strategy

373 backend pytest tests across 31 files. Key clusters:

| Cluster | Files | What it covers |
| --- | --- | --- |
| Workflow contract | `test_workflow_contract.py`, `test_workflow_gate.py`, `test_drafting_gate.py`, `test_synthesis_gate.py` | LangGraph state-machine routing, gate invariants, defensive resume defaults |
| Phase 4 feature pack | `test_phase4_feature_pack.py`, `test_override_workflow.py`, `test_drafting_gate.py` | Export Pack, Citation Manager v1, telemetry, force-approve with reason |
| Phase 3 | `test_analyst_agent.py`, `test_sandbox.py`, `test_phase3_graph_wiring.py`, `test_datasets.py` | Analyst code generation, AST denylist, Docker sandbox isolation, dataset upload + listing |
| Prompt injection | `test_prompt_injection.py` | Poisoned-abstract regression tests across all agents |
| Discovery resilience | `test_discovery_service.py` | Retry-After, retry-exhausted → SourceUnavailableError, XML-bomb rejection |
| Fulltext fetcher | `test_fulltext_fetcher.py` | Concurrency + progress, OA allow-list, redirect chain validation |
| Hardening m1-m4 | `test_hardening_m1..m4_e2e.py` | Auth, rate limits, body cap, e2e workflow walk |
| Audit phase rounds | `test_audit_phase2..phase2_round4.py`, `test_audit_phase4.py` | Per-round regression fixtures |
| Security regression | `test_security_regression.py` | DEV_AUTH_BYPASS guard, workflow rate-limit thresholds, route static checks |
| Cost cap | `test_cost_cap.py` | NFR-5 enforcement at warn + halt |

Plus 17 frontend tests (`frontend/lib/*.test.ts`) for the API client, WS
reconnect contract, and util helpers.

Coverage gate: ≥ 80% line coverage on `backend/app`; the suite currently
reports ~79%.

---

## 16. CI/CD gates and branch protection

### Local + CI gate (`backend/run_ci_local.sh`)

Runs 14 checks in this order:

1. `scripts/preflight.py` — interpreter + required-module + version-drift.
2. `ruff check .`
3. `ruff format --check .`
4. `mypy --strict app/`
5. `pytest -p no:cacheprovider --cov=app --cov-report=term-missing -q`
6. `bandit -q -f json -r app` + JSON-counter gate (fail on MEDIUM+).
7. `scripts/check_radon_budget.sh` (fail on new rank-D unless waivered).
8. `scripts/check_forbidden_patterns.sh` (hex colors, raw SQL, banned imports).
9. `scripts/check_secrets.sh --all`.
10. `pip-audit --strict --requirement <(pip freeze)` (strict in CI mode).
11. `npm audit --omit=dev --audit-level=critical` (CI mode) / `=high` (local warn).
12. `npx tsc --noEmit` (host node_modules or container fallback).
13. `npx next lint`.
14. Coverage threshold ≥ 80%.

CI mode (`CI=1 bash run_ci_local.sh`) flips dep-audit gates to strict.

### Required status checks on `main`

`docs/branch-protection.md` lists 16 required check contexts. Apply with
`scripts/apply_branch_protection.sh owner/repo`:

```
preflight, ruff-check, ruff-format, mypy-strict, pytest, bandit,
radon-budget, forbidden-patterns, secret-scan, pip-audit, frontend-tsc,
frontend-lint, frontend-test, npm-audit, workflow-contract, security-regression
```

Policy: 1 approving review required, dismiss stale reviews on push,
`enforce_admins=true`, no force-push, no deletion of `main`.

### Pre-commit hook (`.husky/pre-commit`)

Runs `scripts/check_secrets.sh` then `lint-staged`. `set -e` so a secret-scan
failure aborts before lint-staged runs.

---

## 17. Local development

### One-shot start (Docker Compose)

```bash
cd C:\Users\Karthi\Desktop\Research-Automation-with-AI
docker compose start     # if containers already built (your normal flow)
# or:
docker compose up -d --build   # first time / after dependency changes
```

URLs:
- Frontend: http://localhost:3000
- Backend: http://localhost:8000
- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/api/v1/health
- Postgres: localhost:5433
- Chroma: http://localhost:8001

Full runbook with start/stop/logs/troubleshooting: `docs/RUNBOOK.md`.

### Required env vars (backend `.env`)

```
APP_ENV=development
LLM_PROVIDER=gemini
LLM_API_KEY=<your gemini key>
LLM_MODEL=gemini-3.5-flash
DATABASE_URL=postgresql+asyncpg://researchflow:researchflow@postgres:5432/researchflow
VECTOR_DB_URL=http://chroma:8000
FIREBASE_PROJECT_ID=<your firebase project>
FIREBASE_CREDENTIALS_PATH=/app/firebase-creds.json
DEV_AUTH_BYPASS=true          # local only — refuses to boot in staging/prod
CORS_ALLOWED_ORIGINS=http://localhost:3000
# Optional (improve discovery yield)
SEMANTIC_SCHOLAR_API_KEY=
CROSSREF_MAILTO=
CORE_API_KEY=
UNPAYWALL_EMAIL=
```

`.env.example` carries the full list. Never commit a real `.env`; the
pre-commit secret-scan blocks API-key patterns.

### Running the suite locally

```bash
cd backend
source .venv/Scripts/activate          # Windows; or .venv/bin/activate on POSIX
bash run_ci_local.sh                    # full 14-gate local CI
# or individual stages:
pytest -q
ruff check . && ruff format --check .
mypy --strict app/
```

---

## 18. Audit history

### Pre-PR-#9 hardening waves (closed)

| Wave | Findings | Outcome |
| --- | --- | --- |
| Wave 1 (HIGH) | A1 prompt injection, A2 citation corrections validation, A3 Next.js CVE bump, A4 defusedxml on arXiv | All 4 closed |
| Wave 2 (MEDIUM) | S1 force-approve reason enforcement, S2 workflow rate limits, S3 Retry-After honor, C1 parallel fulltext + progress, D1 CI gate extensions, D2 branch-protection script | All 6 closed |
| Wave 3 (LOW) | assert→raise (4 sites), B112 logging, TS field tightening, useCallback handlers, WS dedupe, .env.example coverage | All 8 closed |

Final-state delta:
- Bandit: 0/1/6 (H/M/L) → **0/0/0**
- npm audit CRITICAL: 1 → **0**
- Findings matrix (C/H/M/L): 0/4/7/8 → **0/0/0/0**
- pytest: 294 → **321** (then 373 after Phase 3 sprints)
- Fulltext ingest wall-clock: ~120s sequential → ~30s concurrent
- CI gates: 13 → 14

Full ledger in `hardening-closure.md`.

### CodeRabbit rounds on PR #9 (closed via rebase to `main`)

| Round | Verdict | Outcome |
| --- | --- | --- |
| 1 | 15 actionable + 15 minor | 15 fixed, 5 skipped with rationale |
| 2 | 2 Major | 2/2 fixed (`SourceUnavailableError` introduced) |
| 3 | 1 Minor docstring + 2 Major isinstance | 3/3 fixed |
| 4 | 0 actionable + 1 nitpick | 1/1 fixed (`_coerce_id` hoisted to module scope) |

### Phase-3 sprints (closed)

6 sprints over the v0.2 window, all merged to `main`:

1. `edb7b0f` Sprint 1 — dataset upload pipeline.
2. `51416d1` Sprint 2 — Analyst agent + AST denylist.
3. `0eea9a9` Sprint 3 — Docker T2 sandbox + Dockerfile + 9 integration tests.
4. `6aa07a5` Sprint 4 — graph wiring + analysis approve/reject routes + 8 contract tests.
5. `d3aeba9` Sprint 5 — `AnalysisReview` component + analysis API client + WS events.
6. `86020e3` Sprint 6 — SPEC v0.3 + README status + `docs/PHASE3_IMPLEMENTATION_PLAN.md`.

Total Phase-3 footprint: 22 files (12 new, 10 modified), 46 new tests, 4,866 lines added.

---

## 19. Known limitations + accepted risks

### Accepted residual risks

- **4 HIGH advisories on `next@14.2.35`** — image-optimiser remotePatterns DoS,
  RSC HTTP-deserialisation DoS, rewrites smuggling, postcss CSS-stringify XSS.
  None are exploitable here (no `next/image`, no `remotePatterns`, no rewrites
  in `next.config.mjs`, postcss is build-time). Fix is a Next 15.x major bump;
  deferred to v0.3.
- **NFR-6 trace-ID end-to-end** — log fields carry `project_id` and `run_id`
  but not a single request-scoped `trace_id`. UI → API → LLM correlation needs
  a structured trace context (W3C traceparent) wired through structlog. Park
  for v0.3 observability work.
- **NFR-7 WCAG 2.1 AA** — good signals (semantic landmarks, ARIA on action
  buttons, focus rings) but no formal audit yet. Carry-forward.

### Known limitations

- **FR-1.2 user PDF upload** stub (`POST /papers/upload` returns 501). The
  *automatic* OA-PDF path (Unpaywall + fulltext fetcher) is shipped; user-
  initiated upload is v0.3 with browser-use.
- **FR-1.3 Playwright browser automation** absent. Slated for v0.3.
- **Multi-LLM fallback at runtime** — two providers wired (Gemini + Anthropic),
  but the gateway picks one at boot from `LLM_PROVIDER`. Automatic failover
  isn't implemented; would need health checks + circuit-breaker.
- **Sandbox tier T3 (gVisor / Firecracker)** — Docker T2 is the v0.2 launch
  tier. T3 is on the roadmap for v1.0 production hardening.
- **Container warm-pool** for the sandbox — ~500ms container start cost per
  Analyst run is noticeable in UX. v0.3 polish.

---

## 20. Where to go next

### v0.3 candidates (BRD §12)

- **Browser-use scraping (FR-1.3)** — local Playwright instance for papers
  behind login walls or sites without OA mirrors.
- **NFR-6 trace-ID propagation** — wire W3C traceparent header through
  structlog and the LLM gateway.
- **Multi-project dashboard** — currently single-project; the dashboard pulls
  multiple `Project` rows for the same owner.
- **Sandbox warm-pool** — keep 2 idle containers per backend node, rotate on
  use; cuts the ~500 ms cold-start.
- **WCAG audit** — formal pass with axe-core + manual keyboard navigation;
  fix any remaining focus / contrast / aria gaps.

### v1.0 candidates

- **Tauri desktop packaging** — bundle the Next.js client + backend as a
  single desktop binary.
- **Sandbox T3 (gVisor or Firecracker)** — production-grade isolation.
- **Faculty-facing audit-export pipeline** — submission-ready bundle with
  signed AI-disclosure manifest.

### Operational follow-ups

- Apply branch protection on `main` (the rebase landed PR #9's content but
  the GitHub PR closed without the merge button, so branch-protection
  re-apply via `scripts/apply_branch_protection.sh` is still pending).
- Push the `backup/pre-rebase/phase-4` tag to `origin` if you want the
  original-SHA history preserved on the remote.
- Delete the stale local `feature/phase-4` branch after confirming the
  rebase fully reflects on `origin/main`.

---

**Document version:** 1.0 (2026-05-31)
**Author:** Abhijith Anandakrishnan + Claude pair-programming
**Source-of-truth references:** `BRD.md`, `SPEC.md` (v0.3), `ARCHITECTURE.md`,
`hardening-closure.md`, `docs/PHASE3_IMPLEMENTATION_PLAN.md`,
`docs/brd-verification-and-phase3-plan.md`, the 8 alembic migrations,
and live code on `main @ 86020e3`.
