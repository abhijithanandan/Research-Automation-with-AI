# ResearchFlow AI — Sprint Review Report

**Project:** ResearchFlow AI — Agentic Research Automation System
**Review Date:** 19 May 2026
**Branch Reviewed:** feature/phase-1
**Prepared by:** Development Team
**Meeting Type:** Phase Completion Review

---

## Executive Summary

Phase 1 of ResearchFlow AI is **complete and working end-to-end**. The full human-in-the-loop
Discovery workflow — from project creation through Librarian paper fetching, candidate review,
and approval — operates correctly in the browser with a production-quality dark UI, full audit
trail, and all four compliance blockers resolved.

A total of **24 commits** were delivered on feature/phase-1. The system is ready to cut
feature/phase-2.

---

## What Phase 1 Required (BRD Mandate)

Per BRD Section 4.1, Phase 1 must:

- Run the Librarian agent against Semantic Scholar and ArXiv using a user-supplied seed query
- Pause the workflow at a mandatory HITL gate — no automatic advancement
- Present candidate papers for human review with select/deselect capability
- Support three intervention actions: Approve & Proceed, Reject & Regenerate, Manual Override
- Persist every paper, approval decision, and user action to the audit log
- Advance to Phase 2 only after an explicit approve event

All requirements above are now satisfied.

---

## Phase 1 — What Was Built

### 1. Librarian Agent (Backend)

The Librarian is the core AI agent of Phase 1. It takes the user's seed query and does the
following automatically:

**Query Expansion** — Uses Gemini 2.0 Flash to expand the seed query into multiple related
search terms and ArXiv category codes. Example: "fungal image classification" expands into
variations covering deep learning, CNN, mycology, and pathology.

**Multi-source Search** — Queries two academic databases in parallel:
- Semantic Scholar API — title, authors, abstract, citation count, PDF URL
- ArXiv API — preprints with full metadata and direct PDF links

**Deduplication** — Papers appearing in both sources are deduplicated by DOI and
fuzzy title matching (Levenshtein distance). No duplicate paper enters the candidate pool.

**Ranking** — Candidates are ranked by citation velocity (citation count relative to paper
age). More influential papers appear higher in the list.

**Output** — Returns up to 30 ranked candidates with full metadata. All results are persisted
to the database immediately after the Librarian completes.

---

### 2. Four Critical Blockers Fixed (B1 – B4)

These were the compliance blockers that prevented Phase 1 from being declared complete.
All four are now resolved.

---

#### Blocker B1 — Papers Never Saved to Database

**What was wrong:** The Librarian returned paper candidates into LangGraph in-memory state
only. Nothing was written to the database. Every call to GET /papers returned an empty list,
making it impossible to show papers in the UI or build an approved pool.

**What was fixed:** A persist_candidates() function was added that upserts every candidate
paper into the papers table after the Librarian completes. Papers are keyed on
(project_id, citation_key) so re-runs do not create duplicates. All papers are inserted
with approved = false as required by the Librarian contract.

**Impact:** Papers now appear in the UI immediately after the Librarian finishes. The
paper list selector works correctly.

---

#### Blocker B2 — Approved Pool Never Recorded

**What was wrong:** The approved_pool field in the graph state was always an empty list.
When the user clicked Approve, nothing was passed forward. The Critic (Phase 2) would have
no papers to work with. The audit log had no record of which papers were approved.

**What was fixed:** The approve_workflow() function now reads all papers where approved = true
from the database, builds the complete approved pool, and injects it into the LangGraph
state via Command(resume="approve", update={"approved_pool": ...}). Two audit entries are
written: one for the user approve action, one snapshot of all approved citation keys with
a count.

**Impact:** The approved pool is now passed correctly to Phase 2. A full audit trail of
every approval is recorded.

---

#### Blocker B3 — Database State Stuck at "running" While at the Gate

**What was wrong:** LangGraph's interrupt() fires inside the approval gate node, causing
ainvoke() to return. But the workflow_runs.state column in the database was never updated
to "awaiting_approval". When the user clicked Approve, the guard check rejected the call
with: "WorkflowRun is in state 'running', not 'awaiting_approval'."

**What was fixed:** After ainvoke() returns (meaning the graph hit the interrupt), the
background task now opens a fresh database session and updates the run state to
"awaiting_approval" with an awaiting_since timestamp. The approve guard now passes.

**Impact:** Approve, Reject, and Override actions all work correctly. The database state
always reflects the true workflow position.

---

#### Blocker B4 — Override Was a No-Op

**What was wrong:** The POST /workflow/override endpoint redirected to approve without
doing anything. No artifact was saved. No audit entry was written. The override content
was not injected into the graph state. Manual Override was a completely broken feature.

**What was fixed:** override_workflow() now creates an ArtifactRow with produced_by="human",
writes an audit entry with action="user.override" containing the artifact ID, kind, and label,
and injects the artifact dict into the graph state as last_override via Command(resume="approve",
update={"last_override": ...}).

**Impact:** Manual Override is fully operational. Every human-edited artifact is permanently
recorded with full provenance in the audit log.

---

### 3. Infrastructure & Reliability Fixes

#### Users Table — FK Failure on First Request

Every route in the API foreign-key-references the users table. The get_current_user()
dependency decoded the auth token but never wrote a UserRow to the database. Every project
creation call failed with ForeignKeyViolationError on first use.

Fixed by upserting a UserRow on every authenticated request, with db.flush() called
immediately so the row is visible within the same transaction before any downstream INSERT.

---

#### LangGraph Checkpointer — Connection Pool

LangGraph graph nodes run as background asyncio tasks. A single psycopg connection closes
between await boundaries, causing the graph to crash mid-run. Replaced with an
AsyncConnectionPool (max 5 connections) so each background task gets a live connection
from the pool without interference.

This also required splitting psycopg[binary] into psycopg + psycopg-binary as explicit
dependencies, since the combined extra was unreliable on some platforms.

---

#### WebSocket Race Condition

The Librarian runs as a background asyncio task and can finish before the browser's
WebSocket client connects. The approval.required event was being lost — the UI was
permanently stuck at "Librarian is working..." with no way to recover.

Fixed with a replay cache: the last significant event (approval.required, state.changed,
agent.error) is stored per project. When a new WebSocket subscriber connects, the cached
event is immediately replayed into their queue so they catch up regardless of when they
connected.

---

#### UI State Machine — Stuck After Approval

After clicking Approve, all Phase 2+ graph nodes (synthesize, draft_section x7, assemble)
are stubs that complete synchronously. The graph reached END before the WebSocket event
reached the browser. The backend was emitting state: "running" which sent the UI back to
the Librarian spinner with no further event to advance it.

Fixed by emitting state: "approved" after ainvoke() returns from approve (meaning the graph
is at END). The frontend state machine was also corrected to treat "approved" as the done
transition and only return to the running view when phase is "discovery" (the reject path).

---

#### Datetime Timezone Mismatch

All database datetime columns were declared as timezone-naive TIMESTAMP. PostgreSQL and
asyncpg rejected timezone-aware Python datetime objects with a type mismatch error on
every insert. Fixed by applying TIMESTAMP(timezone=True) to every datetime column across
all six ORM models.

---

### 4. HITL Frontend — Complete Phase 1 UI

#### State Machine

The UI implements a strict no-optimistic-UI policy as required by SPEC Section 7.4.
The view only advances when a WebSocket event is received — never on a REST response.

```
idle -> creating -> running -> awaiting -> busy -> done
                       ^                    |
                       |____ reject ________|
```

| View State | User Sees |
|---|---|
| idle | Project creation form |
| creating | Spinner — connecting to backend |
| running | Live agent log — Librarian is fetching |
| awaiting | Paper list with checkboxes + Approval Panel |
| busy | Panel disabled — waiting for graph to advance |
| done | Green card — approved papers, start new project |
| error | Red card — error message, retry button |

---

#### Paper List Selector (BRD FR-1.4)

Every candidate paper is displayed with:

- **Clickable title** — opens the source platform in a new browser tab
- **Source routing** — if a PDF URL exists, opens the PDF directly; otherwise routes to
  arxiv.org/abs/{id} for ArXiv papers or semanticscholar.org/paper/{id} for Semantic Scholar
- **Source badge** — orange "arXiv" badge or blue "Semantic Scholar" badge
- **Authors, year, citation key** — all shown inline
- **Abstract preview** — first two lines, truncated with ellipsis
- **Checkbox** — each toggle calls PATCH /papers/{id} and updates the database immediately

---

#### Three Intervention Actions (BRD Section 4.2)

**Approve & Proceed** — Sends POST /workflow/approve. The graph resumes with the
approved pool injected into state. Transitions to done view on state.changed event.

**Reject & Regenerate** — Opens a feedback textarea. Sends POST /workflow/reject with the
feedback text. The graph loops back to node_discover. The Librarian re-runs with the
same project, and new candidates replace the old ones.

**Manual Override** — Opens a form with three fields: Label (free text), Kind (dropdown:
log / summary / matrix / section / code / figure), Content (large textarea). Sends
POST /workflow/override. Writes an ArtifactRow with produced_by="human" and a full
audit entry. Transitions to done view on state.changed event.

---

### 5. UI Redesign — Production Quality Dark Theme

The frontend was redesigned from a basic utility layout to a production-grade dark research
tool aesthetic.

**Color system:**

| Token | Value | Usage |
|---|---|---|
| Background | #0a0f1e | Page background |
| Surface | #111827 | All cards and panels |
| Border | #1e2d45 | All card borders |
| Accent | #3b82f6 | Primary actions, links |
| Green | #10b981 | Approve, success, done |
| Amber | #f59e0b | Awaiting approval state |
| Red | #ef4444 | Error state |

**Typography:** Inter for UI text, JetBrains Mono for agent log and code.

**Key UI components:**

- Sticky top navigation bar with session status indicator
- Phase Tracker with numbered step circles, connector lines, and blue glow on the active phase
- macOS-style agent log terminal with traffic-light dots and color-coded log lines
- Paper rows with emerald tint on approved items, source badges, external-link icon
- Approval Panel with pulsing amber dot, icon buttons, and animated sub-panels
- All major view transitions use fade-in animation (opacity + translateY)

---

## Phase 1 — Verified Working

The following end-to-end flow was manually verified in the browser:

1. Open http://localhost:3000 — dark UI loads correctly
2. Enter project title and seed query, click Start Librarian
3. Agent log shows librarian started then librarian completed
4. Paper list appears with 20-30 candidates from ArXiv and Semantic Scholar
5. Paper titles open the correct source platform in a new tab
6. Checkboxes toggle paper approval state in the database
7. Approve & Proceed transitions to the green done screen
8. Reject & Regenerate re-runs the Librarian with feedback
9. Manual Override saves a human artifact and transitions to done

---

## Technology Stack — Phase 1

| Layer | Technology |
|---|---|
| Frontend | Next.js 14, React 18, TypeScript, Tailwind CSS |
| Backend | FastAPI, Python 3.11, uvicorn |
| Agent Orchestration | LangGraph 0.2 with interrupt() HITL gates |
| LLM | Gemini 2.0 Flash via google-genai SDK |
| Database | PostgreSQL 16 via SQLAlchemy asyncpg |
| Checkpointer | LangGraph AsyncPostgresSaver + psycopg-pool |
| Vector Store | ChromaDB 0.5 (ready for Phase 2) |
| Paper Sources | Semantic Scholar API, ArXiv API |
| Deduplication | thefuzz + python-Levenshtein |
| Auth (dev) | DEV_AUTH_BYPASS=true, any Bearer token accepted |
| Infrastructure | Docker Compose (backend, frontend, postgres, chroma) |

---

## What Remains Before Cutting feature/phase-2

| Item | Status |
|---|---|
| psycopg-binary baked into Docker image via --build | Pending |
| Firebase Auth wired for production use | Deferred |
| Crossref API integration in Librarian | Deferred |
| WCAG 2.1 AA accessibility audit | Deferred to pre-v1.0 |

None of the above block Phase 2 from starting.

---

## Phase 2 — Planned Next (Minimal Scope)

Phase 2 implements the Critic agent (BRD Section 4.2 — Literature Synthesis).

**What the Critic will do:**
- Read the approved paper pool from state["approved_pool"]
- Extract per-paper: problem statement, method, dataset, key results, limitations
- Produce a comparison matrix (structured JSON rendered as Markdown table)
- Produce a narrative synthesis paragraph
- Pause at the Phase 2 HITL gate for human review

**Deliverables for Phase 2:**
- node_synthesize implementation (replaces current stub)
- Critic agent with RAG context from ChromaDB
- Diff/edit view in the frontend for the matrix and summary
- Phase 2 approval gate using the same interrupt() pattern

---

## Phase 3 — Planned (Minimal Scope)

Phase 3 implements the Analyst agent (BRD Section 4.3 — Data Analysis). This phase is
optional per BRD MVP scope and will be built in v0.2.

**What the Analyst will do:**
- Accept a dataset reference from the user
- Write Python code in a sandboxed execution environment
- Produce figures, tables, and a methods narrative
- Show the code to the user before execution (mandatory per BRD)
- Pause at the Phase 3 HITL gate

---

## Appendix — Running Phase 1 Locally

```
1. git checkout feature/phase-1
2. Create backend/.env with LLM_API_KEY=<gemini-key> and DEV_AUTH_BYPASS=true
3. Create frontend/.env.local with NEXT_PUBLIC_DEV_TOKEN=dev-token
4. docker compose up -d --build
5. docker compose exec backend pip install psycopg-binary
6. docker compose restart backend
7. Open http://localhost:3000
```

Health check — all three lines must appear in backend logs:

```
cleaned_up_orphaned_workflow_runs
checkpointer_postgres_ready
graph_compiled
```

---

*ResearchFlow AI — Phase 1 Review Report — 19 May 2026*
