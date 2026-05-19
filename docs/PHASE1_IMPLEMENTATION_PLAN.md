# ResearchFlow AI — Phase 1 Implementation Plan & Progress Record

**Branch:** `feature/phase-1`
**Base reference:** `main` (read-only template — BRD.md + ARCHITECTURE.md + SPEC.md)
**Date recorded:** 2026-05-19
**Status:** Phase 1 complete and working end-to-end

---

## 1. What Phase 1 Is

Per **BRD §4.1**, Phase 1 is the *Query & Discovery* phase:

> The Librarian fetches candidate papers from Semantic Scholar and ArXiv. The system pauses. The user reviews, selects/deselects, and approves the working paper pool. No phase advances without an explicit `approve` event.

This is the first of four HITL-gated phases. Completing it means the full round-trip — project creation → Librarian run → paper review → approval — works without error, with the audit trail intact, and with the database state matching the UI state.

---

## 2. Starting Point (main branch scaffold)

The `main` branch was committed as a template/reference and contained:

| Component | What was there |
|---|---|
| `backend/` | FastAPI + LangGraph skeleton, stub agent nodes, Alembic migrations, Dockerfile |
| `frontend/` | Next.js 14 App Router scaffold, Tailwind, TypeScript types mirroring SPEC |
| `SPEC.md` | Full API + WS + state-machine contract (source of truth) |
| `BRD.md` | Business + functional requirements, HITL mandate, 4-phase roadmap |
| `ARCHITECTURE.md` | Hybrid client-server diagram, data-flow, deployment topology |
| `docker-compose.yml` | Postgres 16, ChromaDB 0.5, backend, frontend services |

No Phase 1 logic was implemented — the Librarian agent, approval endpoints, DB persistence, and frontend HITL UI were all stubs or absent.

---

## 3. All Work Done on feature/phase-1

### 3.1 Commit History (oldest → newest)

| Commit | Description |
|---|---|
| `00c9d9c` | Initial scaffold: FastAPI + LangGraph remote engine (from main) |
| `8951537` | Initial scaffold: Next.js 14 client (from main) |
| `fea3b9d` | Husky + lint-staged + commitlint + Prettier (from main) |
| `a052d65` | Librarian: citation velocity ranking, key collision fix |
| `36ea695` | Librarian: citation_count tracking, ArXiv age filter |
| `3733da0` | Phase 1 security + architecture remediations |
| `cfabdbe` | CI: lazy-load Gemini client, frontend package-lock |
| `7aa27c2` | Tests: `cn` class-merging helper unit tests |
| `f1b6268` | Security: remove raw credentials from dev configs |
| `a6550ff` | Style: format list_models.py with ruff |
| `b9bde9d` | Refactor: systematic fix of 16 CodeRabbit review findings |
| `37a4fb0` | Fix: WebSocket auth + workflow pool gate decisions |
| `8440c91` | **Fix: close all four Phase-1 blockers B1–B4** |
| `5375c72` | **Fix: M5 users upsert + Phase 1 Approval UI** |
| `e7d4376` | **Fix: resolve all runtime errors from smoke testing** |
| `35bec9b` | **Fix: WS race condition — late-subscriber replay cache** |
| `bc96f99` | **Fix: emit `state.approved` after graph reaches END; fix UI state machine** |
| `aa75e8e` | **Feat: clickable paper title links to source platform** |
| `1c5ba5d` | **Feat: comprehensive frontend redesign** |

---

## 4. The Four Phase-1 Blockers (B1–B4)

These were identified in `phase_1_compliance_report.md` and closed in commit `8440c91`.

### B1 — §4.1: Librarian candidates never persisted to DB

**Problem:** `node_discover` wrote papers to `state["candidates"]` (in-memory LangGraph state only) but never upserted them into the `papers` DB table. Any `GET /papers` call returned empty.

**Fix — `backend/app/services/workflow.py`:**
- Added `_persist_candidates()` helper that upserts each paper as a `PaperRow` keyed on `(project_id, citation_key)`.
- Called from `_run_graph()` after `ainvoke()` returns (after the graph hits `interrupt()`).
- All rows inserted with `approved=False` — invariant from the Librarian contract.

---

### B2 — §4.2: `approve_workflow` never built the approved-pool snapshot

**Problem:** `state["approved_pool"]` was initialised to `[]` and never written. No `AuditLogEntry` for the approved pool.

**Fix — `backend/app/services/workflow.py`:**
- `approve_workflow()` now queries all `PaperRow` where `approved=True` for the project, builds a full dict list, and passes it to `Command(resume="approve", update={"approved_pool": approved_pool})`.
- Two audit entries written: `user.approve` (user action) and `phase_1.approved_pool` (snapshot of citation keys and count).

---

### B3 — §4.3: Background dispatch race — DB state stayed `"running"` at the gate

**Problem:** `_run_graph()` ran as a background `asyncio.create_task`. When LangGraph's `interrupt()` fired, `ainvoke()` returned but the DB `workflow_runs.state` column was never updated to `"awaiting_approval"`. The `approve_workflow()` guard (`_assert_awaiting`) then rejected the approve call.

**Fix — `backend/app/services/workflow.py`:**
- After `ainvoke()` returns (= graph hit interrupt), `_run_graph` opens a fresh DB session and calls `_update_run_state(bg_session, run_id, "awaiting_approval")`.
- `_assert_awaiting()` guard now passes because the DB state matches.
- `GraphInterrupt` is not raised externally — LangGraph handles it internally; `ainvoke()` simply returns at the interrupt point.

---

### B4 — §4.4: `/workflow/override` was a no-op redirect

**Problem:** The override endpoint redirected to approve without: writing an `ArtifactRow` with `produced_by="human"`, recording `action="user.override"` in the audit log, or passing the artifact into graph state.

**Fix — `backend/app/services/workflow.py` + `backend/app/api/routes/workflow.py`:**
- `override_workflow()` now creates an `ArtifactRow(produced_by="human")`.
- Writes an audit entry `action="user.override"` with artifact ID, kind, and label.
- Passes the artifact dict into `Command(resume="approve", update={"last_override": artifact_state})`.
- Override payload fields: `artifact_kind`, `label`, `content`, `mime_type`.

---

## 5. Additional Fixes (M5 + Runtime Errors)

### M5 — Users never upserted on first request

**Problem:** `get_current_user()` in `backend/app/api/deps.py` decoded the Firebase token and returned a `User` schema object but never wrote a `UserRow` to the DB. Any route that FK-referenced `users.id` (e.g., project creation) failed with `ForeignKeyViolationError`.

**Fix — `backend/app/api/deps.py`:**
```python
existing = await db.scalar(select(UserRow).where(UserRow.firebase_uid == uid))
if existing is None:
    db.add(UserRow(id=user_id, firebase_uid=uid, email=email, ...))
    await db.flush()   # must be visible before any FK-referencing INSERT
else:
    existing.email = email
    existing.display_name = display_name
```

---

### Datetime timezone mismatch

**Problem:** All `Mapped[datetime]` columns used plain `TIMESTAMP` (timezone-naive). PostgreSQL + asyncpg rejected timezone-aware Python `datetime` objects with `DataError: can't subtract offset-naive and offset-aware datetimes`.

**Fix — `backend/app/models/db.py`:**
```python
from sqlalchemy import TIMESTAMP
_TS = TIMESTAMP(timezone=True)

class UserRow(Base):
    created_at: Mapped[datetime] = mapped_column(_TS)
# Applied to ALL datetime columns in all ORM models
```

---

### FK ordering within session (audit log)

**Problem:** `AuditLogRow` inserted before the parent `ProjectRow` was flushed. FK constraint failed.

**Fix — `backend/app/api/routes/projects.py`:**
```python
db.add(row)
await db.flush()   # ensure ProjectRow visible before audit FK reference
db.add(AuditLogRow(...))
```

---

### LangGraph checkpointer — connection pool

**Problem:** `AsyncPostgresSaver` was created with a single psycopg connection. Background asyncio tasks (the LangGraph graph runner) caused `"the connection is closed"` errors because a single connection closes between asyncio `await` boundaries.

**Fix — `backend/app/graph/workflow.py`:**
```python
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

pool = AsyncConnectionPool(conninfo=pg_url, max_size=5, open=False)
await pool.open()
saver = AsyncPostgresSaver(conn=pool)
await saver.setup()
```

Also updated `pyproject.toml`:
```
"psycopg>=3.1",
"psycopg-binary>=3.1",   # explicit binary wheel
"psycopg-pool>=3.2",
```

---

### WS race condition — approval.required missed by late subscriber

**Problem:** The Librarian (running as a background task) could complete and emit `approval.required` before the frontend WebSocket client had connected. The event was lost; the UI stuck at "Librarian is working…" indefinitely.

**Fix — `backend/app/services/workflow.py`:**
```python
_REPLAY_TYPES = {"approval.required", "state.changed", "agent.error"}
_last_event: dict[UUID, dict[str, object]] = {}

def subscribe_project(project_id: UUID) -> asyncio.Queue:
    q = asyncio.Queue(maxsize=256)
    _ws_event_bus[project_id] = q
    # Replay the last significant event to the new subscriber
    cached = _last_event.get(project_id)
    if cached is not None:
        q.put_nowait(cached)
    return q

async def _emit(project_id, event):
    event["ts"] = datetime.now(tz=UTC).isoformat()
    if event.get("type") in _REPLAY_TYPES:
        _last_event[project_id] = event   # cache for late subscribers
    ...
```

---

### UI state machine — stuck after approve (state.changed regression)

**Problem:** After `approve`, all Phase 2+ nodes (synthesize → draft_section × 7 → assemble) are stubs that complete synchronously. `ainvoke()` returns only after the graph reaches END. The backend was emitting `state.changed{state:"running"}` which sent the frontend back to the "Librarian is working…" spinner with no further event to advance it.

**Fix — `backend/app/services/workflow.py`:**
Changed the post-approve emit from `state: "running"` → `state: "approved"`:
```python
await _emit(project_id, {
    "type": "state.changed",
    "phase": Phase.SYNTHESIS.value,
    "state": "approved",   # was "running" — graph is already at END
    "run_id": str(run_id),
})
```

**Fix — `frontend/app/page.tsx`** state.changed handler:
```typescript
if (evt.state === "approved" || evt.phase === "done") {
    setView("done");
} else if (evt.state === "running" && evt.phase === "discovery") {
    setView("running");   // only reject sends us back to discovery
}
```

---

## 6. Infrastructure Changes

### docker-compose.yml

- Changed Postgres host port `5432:5432` → `5433:5432` to avoid conflict with any local Postgres installation.
- All services: backend, frontend, postgres, chroma defined with named volumes for persistence.

### backend/.env (not committed — in .gitignore)

```
APP_ENV=development
LOG_LEVEL=INFO
CORS_ALLOWED_ORIGINS=http://localhost:3000
LLM_PROVIDER=gemini
LLM_API_KEY=<gemini-api-key>
LLM_MODEL=gemini-2.0-flash
DATABASE_URL=postgresql+asyncpg://researchflow:researchflow@postgres:5432/researchflow
VECTOR_DB_URL=http://chroma:8000
VECTOR_DB_PROVIDER=chroma
DEV_AUTH_BYPASS=true
DEFAULT_TOKEN_CAP_USD=5.0
MAX_PAPER_CANDIDATES=30
TOKEN_CAP_WARN_PCT=0.8
```

### LLM model selection

- Started with `gemini-2.5-pro` — hit free-tier daily quota.
- Switched to `gemini-2.0-flash` — also hit quota after repeated testing.
- Resolved by using a fresh Gemini API key with quota available.

---

## 7. Frontend — Phase 1 HITL UI

### 7.1 Page state machine (`frontend/app/page.tsx`)

```
idle → creating → running → awaiting → busy → done | error
                                ↑______________|
                                (reject loops back)
```

| State | Trigger | What user sees |
|---|---|---|
| `idle` | Initial load | Project creation form |
| `creating` | Form submit | Spinner "Creating project…" |
| `running` | `workflow/start` succeeds | Agent log, "Librarian is fetching…" |
| `awaiting` | `approval.required` WS event | Paper list + approval panel |
| `busy` | Any action button clicked | Approval panel disabled, spinner |
| `done` | `state.changed{state:"approved"}` | Green success card with paper list |
| `error` | Any exception | Red error card with retry button |

**Key implementation decisions:**
- WS connection opened **before** `POST /workflow/start` to avoid the race condition.
- View advances **only on WS events**, never on REST responses (SPEC §7.4 no-optimistic-UI rule).
- Papers fetched from `GET /papers` on `approval.required`, not before.

### 7.2 Paper list selector (BRD FR-1.4)

- Each paper row shows: title (clickable link), authors, year, source badge (arXiv / Semantic Scholar), citation key, abstract preview.
- Checkbox calls `PATCH /papers/{id}` to toggle `approved` flag in DB.
- `paperSourceUrl()` resolves: `pdf_url` (if present) → `arxiv.org/abs/{id}` → `semanticscholar.org/paper/{id}` → `doi.org/{id}`.
- Clicking title opens source in new tab; does not toggle checkbox (`stopPropagation`).

### 7.3 ApprovalPanel — all three BRD §4.2 intervention actions

| Action | What it does |
|---|---|
| **Approve & proceed** | `POST /workflow/approve` → graph resumes with approved pool |
| **Reject & regenerate** | Shows feedback textarea → `POST /workflow/reject` → graph loops back to `discover` |
| **Manual override** | Shows form (label, kind selector, content textarea) → `POST /workflow/override` → writes ArtifactRow + audit entry |

### 7.4 Frontend redesign (commit `1c5ba5d`)

- **Theme:** Dark navy (`#0a0f1e` base, `#111827` surface) with blue accent glows.
- **Typography:** Inter (UI) + JetBrains Mono (agent log, code) from Google Fonts.
- **Layout:** Sticky top nav with session indicator, single-column card layout (max-width 672px).
- **AgentLog:** macOS-style terminal with traffic-light dots; blue for `agent.started`, green for `agent.completed`, red for `agent.error`.
- **Paper rows:** Emerald highlight on approved papers, source badges (arXiv orange, Semantic Scholar blue), external-link SVG icon on title.
- **PhaseTracker:** Numbered step circles with connector lines, blue glow ring on active phase, green fill for completed phases.
- **ApprovalPanel:** Amber pulsing dot on header, icon buttons with hover glow effects, fade-in sub-panels for reject/override forms.
- **Animations:** `animate-fade-in` (translateY + opacity), `animate-pulse-dot` (scale pulse), `animate-spin` on loading spinners.

---

## 8. Backend Architecture Summary

```
frontend (Next.js 14)
    │  REST /api/v1/*
    │  WebSocket /api/v1/projects/{id}/events
    ▼
FastAPI (uvicorn)
    ├── /api/routes/projects.py    — CRUD for projects
    ├── /api/routes/workflow.py    — start / approve / reject / override
    ├── /api/routes/papers.py      — list / PATCH approved flag
    ├── /api/routes/websocket.py   — WS connection handler
    ├── /api/routes/artifacts.py   — artifact CRUD
    └── /api/routes/health.py      — liveness probe
    │
    ├── /services/workflow.py      — orchestration: _run_graph, approve, reject, override
    │       ├── asyncio event bus (_ws_event_bus)
    │       ├── replay cache (_last_event) — fixes WS race
    │       ├── _persist_candidates() — B1 fix
    │       └── audit log writes
    │
    ├── /graph/workflow.py         — LangGraph graph definition
    │       ├── node_discover      — calls Librarian agent
    │       ├── node_await_pool_approval — interrupt() gate
    │       ├── node_synthesize    — Phase 2 stub
    │       ├── node_draft_section — Phase 4 stub
    │       └── node_assemble      — assembles final doc (stub)
    │
    ├── /agents/librarian.py       — Semantic Scholar + ArXiv search,
    │                                Gemini query expansion, dedup, ranking
    │
    ├── /models/db.py              — SQLAlchemy ORM models
    │       UserRow, ProjectRow, WorkflowRunRow,
    │       PaperRow, ArtifactRow, AuditLogRow
    │       (all datetime columns: TIMESTAMP(timezone=True))
    │
    └── /db/session.py             — AsyncSession factory (asyncpg)
```

---

## 9. Database Schema (Phase 1 relevant tables)

```sql
users           (id, firebase_uid, email, display_name, created_at)
projects        (id, owner_id→users, title, seed_query, status, current_phase, ...)
workflow_runs   (id, project_id→projects, phase, state, checkpoint_id,
                 started_at, awaiting_since, last_event_at)
papers          (id, project_id→projects, source, external_id, title, authors,
                 year, abstract, pdf_url, citation_key, citation_count,
                 approved, added_at)
artifacts       (id, project_id→projects, kind, label, content, mime_type,
                 produced_by, parent_id, created_at)
audit_log       (id, project_id→projects, workflow_run_id→workflow_runs,
                 actor, action, payload, created_at)
```

---

## 10. WebSocket Event Contract (SPEC §4)

All events emitted by `_emit()` in `workflow.py`:

| Event type | When emitted | Key fields |
|---|---|---|
| `agent.started` | Before `ainvoke()` | `agent`, `run_id` |
| `agent.token` | During LLM streaming (if enabled) | `agent`, `delta` |
| `agent.completed` | After `ainvoke()` returns | `agent`, `run_id` |
| `agent.error` | On exception in `_run_graph` | `agent`, `error` |
| `approval.required` | After candidates persisted, gate waiting | `phase`, `run_id`, `summary` |
| `state.changed` | After approve / reject / override | `phase`, `state`, `run_id` |

Events in `_REPLAY_TYPES` (`approval.required`, `state.changed`, `agent.error`) are cached in `_last_event` and replayed to late WS subscribers.

---

## 11. REST API Endpoints Used in Phase 1

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/projects` | Create project (returns project.id) |
| `POST` | `/api/v1/projects/{id}/workflow/start` | Start Librarian, create WorkflowRun |
| `GET` | `/api/v1/projects/{id}/papers` | List candidate papers |
| `PATCH` | `/api/v1/projects/{id}/papers/{paper_id}` | Toggle paper.approved flag |
| `POST` | `/api/v1/projects/{id}/workflow/approve` | Resume graph with approve |
| `POST` | `/api/v1/projects/{id}/workflow/reject` | Resume graph with reject + feedback |
| `POST` | `/api/v1/projects/{id}/workflow/override` | Submit human artifact, resume graph |
| `WS` | `/api/v1/projects/{id}/events` | Subscribe to live workflow events |

---

## 12. What Phase 1 Does NOT Include (out of scope, deferred)

Per BRD §8 MVP scope and the phase boundary:

| Feature | Deferred to |
|---|---|
| Phase 2 — Critic / synthesis | Phase 2 PR |
| Phase 3 — Analyst / sandboxed code | v0.2 |
| Phase 4 — Scribe / section drafting | Phase 4 PR |
| Browser-use Playwright scraping | v0.2 |
| Firebase Auth (real OAuth) | Before production |
| LaTeX output | v0.2 |
| Multi-project dashboard | v0.3 |
| Token/cost dashboard | v0.3 |
| Local PDF upload + parsing | Phase 2+ |
| Crossref integration | Phase 1+ (Librarian already has the hook) |
| WCAG 2.1 AA accessibility audit | Pre v1.0 |

---

## 13. Known State at Phase 1 Close

| Item | Status |
|---|---|
| B1 — Candidates persisted to DB | ✅ Fixed |
| B2 — Approved pool snapshot + audit | ✅ Fixed |
| B3 — DB state `awaiting_approval` at gate | ✅ Fixed |
| B4 — Override writes artifact + audit | ✅ Fixed |
| M5 — Users upserted on first request | ✅ Fixed |
| Datetime timezone mismatch | ✅ Fixed |
| LangGraph connection pool | ✅ Fixed |
| WS race condition (replay cache) | ✅ Fixed |
| UI state machine post-approve | ✅ Fixed |
| Clickable paper links | ✅ Implemented |
| Frontend redesign (dark theme) | ✅ Implemented |
| `psycopg-binary` in pyproject.toml | ✅ Fixed |
| `DEV_AUTH_BYPASS` for local dev | ✅ Working |
| Postgres checkpointer (not MemorySaver) | ✅ Working |
| End-to-end flow tested in browser | ✅ Verified |

---

## 14. How to Run Phase 1 Locally

### Prerequisites
- Docker Desktop running
- Gemini API key with available quota

### Steps

```bash
# 1. Clone / checkout
git checkout feature/phase-1

# 2. Create backend/.env (never commit this)
cat > backend/.env << 'EOF'
APP_ENV=development
LOG_LEVEL=INFO
CORS_ALLOWED_ORIGINS=http://localhost:3000
LLM_PROVIDER=gemini
LLM_API_KEY=<your-gemini-api-key>
LLM_MODEL=gemini-2.0-flash
DATABASE_URL=postgresql+asyncpg://researchflow:researchflow@postgres:5432/researchflow
VECTOR_DB_URL=http://chroma:8000
VECTOR_DB_PROVIDER=chroma
DEV_AUTH_BYPASS=true
DEFAULT_TOKEN_CAP_USD=5.0
MAX_PAPER_CANDIDATES=30
TOKEN_CAP_WARN_PCT=0.8
EOF

# 3. Create frontend/.env.local (never commit this)
cat > frontend/.env.local << 'EOF'
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
NEXT_PUBLIC_WS_BASE_URL=ws://localhost:8000
NEXT_PUBLIC_DEV_TOKEN=dev-token
EOF

# 4. Start all services
docker compose up -d --build

# 5. Install psycopg-binary in running backend container
#    (until next --build bakes it in permanently)
docker compose exec backend pip install psycopg-binary

# 6. Restart backend to activate Postgres checkpointer
docker compose restart backend

# 7. Open browser
# http://localhost:3000
```

### Verifying it works

1. Open http://localhost:3000 — dark navy UI loads.
2. Enter a project title and seed query (e.g. "fungal image classification").
3. Click **Start Librarian** — agent log shows `▶ librarian started`.
4. After ~5–15 seconds: `✓ librarian completed` and the paper list appears.
5. Check papers, click titles to verify external links open correctly.
6. Click **Approve & proceed** — green "Phase 1 complete" card appears with the approved paper list.
7. To test reject: click **Reject & regenerate**, enter feedback, submit — the Librarian re-runs.
8. To test override: click **Manual override**, fill in label/kind/content, submit — the done card appears.

### Backend health check

```bash
# All three startup lines should appear:
docker compose logs backend | grep -E "orphaned|postgres_ready|graph_compiled"
# Expected:
# {"event": "cleaned_up_orphaned_workflow_runs", ...}
# {"event": "checkpointer_postgres_ready", ...}
# {"event": "graph_compiled", ...}
```

---

## 15. Next Steps — Cutting feature/phase-2

Before creating `feature/phase-2`, the following should be confirmed:

1. **All four blockers (B1–B4) verified in DB** — query `audit_log` and `papers` tables directly to confirm rows exist after a full approval run.
2. **Override flow verified** — check `artifacts` table has a row with `produced_by='human'` and `audit_log` has `action='user.override'`.
3. **`psycopg-binary` baked into Docker image** — run `docker compose up -d --build` once so the pip-install workaround is no longer needed.
4. **Phase 2 design** — Critic agent: reads approved pool from DB, builds comparison matrix (structured JSON), produces narrative summary. Gate before Phase 4 (Scribe).
5. **SPEC update** — SPEC.md §5 (Critic I/O contract) and §4 (WS events for Phase 2) should be updated before implementation begins.

---

*This document was generated from git history, source code, and session notes on 2026-05-19.*
