# BRD Verification + Phase 3 Plan — `feature/phase-4 @ 765002d`

**Date:** 2026-05-31
**Verdict (TL;DR):** Build is **fully aligned with BRD v0.1 (MVP) scope**. Phase 3 (Analyst / sandboxed compute) is **deferred by design** — BRD §8 explicitly excludes it from MVP and §12 schedules it for v0.2 (Q4 2026). The skip is **not a gap, it's the plan**. This doc verifies that nothing else has drifted, then designs the Phase 3 path so the next sprint can start it.

---

## Part 1 — Current state vs BRD/FRD/SPEC

### 1.1 MVP scope (BRD §8) coverage

| BRD §8 item | Status | Evidence |
| --- | --- | --- |
| Single-user, single-project | ✅ | One project flow; no multi-project routing. |
| Phases 1, 2, 4 (skip Analyst/Phase 3) | ✅ | `app/agents/analyst.py:30` raises `NotImplementedError("Analyst is scheduled for v0.2")`. Graph wires `discover→synthesize→draft→assemble`; no `analyze` edge in `app/graph/workflow.py`. |
| Semantic Scholar + ArXiv | ✅ (over-delivered) | 5 adapters in `services/discovery.py`: SS, arXiv, Crossref, CORE, Europe PMC. |
| Markdown output (LaTeX in v0.2) | ✅ | Scribe rejects `output_format=="latex"`. Export Pack offers `markdown / bibtex / package(ZIP) / bundle` — no LaTeX. |
| Chroma + Postgres + one LLM | ✅ (over-delivered) | Chroma vector store + Postgres checkpointer; 2 live LLM providers (Gemini + Anthropic) via `services/llm.py`. |
| Out-of-scope (correctly absent) | ✅ | No Playwright; no LaTeX; no multi-LLM fallback at runtime; no multi-project dashboard. `POST /papers/upload` returns 501. |

### 1.2 Functional requirements (BRD §5)

| FR | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| **FR-1.1** | Dashboard: phase state, current phase, pending approvals, agent activity, token spend | ✅ | `PhaseTracker`, `AgentLog`, `ApprovalPanel`, `DraftingTelemetryChips` (sections drafted, avg ms, regenerations, overrides, cite-fixes). Token-spend via `/usage` rollup + `cost.cap_warn`/`cap_exceeded` WS events. |
| **FR-1.2** | Local PDF upload + parse → chunk → embed | ⚠️ Deferred | `POST /papers/upload` = **501** (BRD §8 marks PDF upload out of MVP). The *automatic* OA-PDF path is implemented (`services/fulltext_fetcher.py` → ChromaDB) and runs concurrently with WS progress events (W2-C1). User-initiated upload remains v0.2. |
| **FR-1.3** | Local Playwright browser automation | ⛔ Deferred | BRD §12 schedules browser-use for v0.3. No Playwright in tree. |
| **FR-1.4** | Approval UI: paper selector (P1), diff/edit (P2/P4), plot/code (P3) | ✅ for P1/P2/P4 | `ApprovalPanel`, `SynthesisReview` (matrix + narrative + diff + override), `SectionReview` (per-section + diff-vs-previous + citations tab + override editor). P3 plot/code viewer correctly absent. |
| **FR-1.5** | Citation manager: inline BibTeX preview + manual correction before approve | ✅ | Citations tab in `SectionReview` shows resolved citations (title, authors, year, source URL) from `GET /drafting/citations`. Unresolved keys flagged. Override panel accepts `citation_corrections` map; server validates against approved pool (W1-A2). |
| **FR-2.1** | Librarian (Discovery) | ✅ | Multi-source fan-out, fuzzy dedup, citation velocity ranking. XML-encapsulated prompts (W1-A1). `defusedxml` on arXiv parsing (W1-A4). Retry-After honored on 429 (W2-S3). |
| **FR-2.2** | Critic (Reviewer) | ✅ | Matrix + narrative synthesis from approved pool. RAG context from Chroma. XML-encapsulated prompts (W1-A1). |
| **FR-2.3** | Analyst (Compute) | ⛔ Deferred to v0.2 | Stub raises `NotImplementedError`. Sandbox infrastructure not built. **This is the topic of Part 2 below.** |
| **FR-2.4** | Scribe (Writer) | ✅ | Per-section Markdown drafting, RAG-grounded, cite-only-from-approved-pool enforced post-generation with one retry + `INVALID:` surfacing. |
| **FR-3.1** | LangGraph workflow engine + approval gates | ✅ | 7 nodes wired, 3 HITL `interrupt()` gates, defensive resume defaults (audit finding #6). |
| **FR-3.2** | Vector storage (RAG, per-project namespacing) | ✅ | Chroma with `project_id` namespace. |
| **FR-3.3** | Token/cost management | ✅ | `audit_log` records model + tokens_in + tokens_out + cost_usd. `/usage` aggregates per-project. Cost cap (NFR-5) enforced. |
| **FR-3.4** | Persistence (Postgres + audit log) | ✅ | 7 alembic migrations; 4 partial unique indexes; CHECK constraint on `workflow_runs.state`. |
| **FR-3.5** | Auth (Firebase Google OAuth) | ✅ | Firebase token verification + DB user-upsert; HTTP and WS auth resolve to the same identity. |

### 1.3 Non-functional requirements (BRD §6)

| NFR | Requirement | Status |
| --- | --- | --- |
| **NFR-1 Modularity** | Frontend↔backend decoupled via REST + WS only | ✅ SPEC.md v0.2 + 14 typed endpoints + 9 WS event types. |
| **NFR-2 Latency** | Streaming progress; P95 time-to-first-token ≤ 3s | ✅ WS streams `agent.started/token/completed`. Fulltext fetch now parallelized (~120s → ~30s). |
| **NFR-3 Security & privacy** | Per-user namespacing; zero-retention LLM config | ✅ Per-project Chroma namespace; LLM provider abstraction so retention flags are configurable. |
| **NFR-4 Reproducibility** | Version-controlled prompt templates | ✅ All prompts in source (`_PROMPT_TEMPLATE`, `_EXTRACTION_PROMPT_TEMPLATE`). |
| **NFR-5 Cost cap** | Halt + warn at 80% | ✅ `_enforce_cost_cap` in `services/workflow.py` — emits `cost.cap_warn` at threshold, halts run on cap reached. |
| **NFR-6 Observability** | Structured JSON logs + trace IDs | ✅ structlog JSON output. Trace-ID propagation is **partial** — log fields include `error_type`, `project_id`, `run_id`, `actor`. End-to-end UI→API→LLM trace correlation not yet wired (carry-over from Phase-1 audit). |
| **NFR-7 Accessibility** | WCAG 2.1 AA | ⚠️ Good signals (semantic landmarks, ARIA on buttons, focus rings) but **no formal audit**. Carry-forward. |

### 1.4 Success metrics (BRD §9) — current measurability

| Metric | Target | Can we measure it now? |
| --- | --- | --- |
| Time to first usable draft | ≤ 45 min | ✅ via timestamps in `workflow_runs.started_at` → first `phase_4.section_ready` audit row. |
| Approval-gate compliance | 100% | ✅ audit-log assertion: every `state.changed` to non-awaiting must be preceded by a `user.approve` or `user.reject` row. Test coverage exists. |
| Citation accuracy | ≥ 95% from approved pool | ✅ Scribe validator + post-assemble `_build_references_section` flags unresolved keys. FR-1.5 manager surfaces them. |
| User-reported time saved | ≥ 60% | ⛔ No in-app survey yet. v0.3 dashboard work. |
| Cost per lit review | ≤ USD 5 | ✅ `Project.token_cap_usd` default 5.0; `/usage` reports actual. |

### 1.5 Risks (BRD §10) — mitigation status

| Risk | BRD mitigation | Actual state |
| --- | --- | --- |
| Hallucinated citations | Cite-only-from-pool + post-gen validator | ✅ Scribe retry + INVALID surfacing + W1-A2 server-side validation of corrections |
| Academic integrity | Audit trail + AI-disclosure appendix | ✅ Export Pack ships `ai-disclosure.md` + `audit-appendix.md` |
| Bot detection | Local browser + rate limit | ⛔ Browser-use not built; API-only path used |
| Cost spikes | Per-project cap + cheap-model tier | ✅ Cap enforced; tiered model not implemented but single-model cost is well under cap |
| Vendor lock-in | Pluggable abstraction | ✅ Gemini + Anthropic both live |
| **Sandbox escape (Analyst)** | Phase 3 deferred to v0.2 + security review gate | ⛔ **By design — not built yet. See Part 2.** |

### 1.6 Verdict on direction

**Yes, the build is on the right track.** The audit branch closed all 4 HIGH + 7 MEDIUM + 8 LOW findings; bandit/mypy/ruff/eslint/tsc all clean; 321 pytest passing; CI script gates 14 checks. The only structural BRD gap is FR-2.3 (Analyst / Phase 3) — and that was always v0.2 scope. **No detour is needed.** We are exactly where the BRD roadmap says we should be.

---

## Part 2 — Phase 3 (Analyst) Implementation Plan

### 2.1 What Phase 3 actually requires (BRD §4.1, FR-2.3, §10 risk)

> **FR-2.3 The Analyst (Compute):** A sandboxed Python execution environment (process-isolated, network-restricted, time-limited). Receives a task description and a dataset reference; produces code, executes it, and returns figures, tables, and stdout. **Code is always shown to the user before execution.**

> **BRD §4.1 Phase 3:** *"Analyst writes and executes code in a sandbox to produce figures, tables, and a methods narrative. System pauses. User reviews the generated artifacts and code log."*

> **BRD §10 risk row (sandbox escape):** *"Phase 3 deferred to v0.2 and gated by a security review; user must approve code before execution."*

Key facts:
1. Phase 3 is **optional** in the workflow — projects without a dataset just skip from Phase 2 (synthesis) directly to Phase 4 (drafting). This is already how the graph is wired today.
2. **Two HITL gates** are needed: (a) user reviews generated **code** before execution, (b) user reviews generated **figures/tables/methods** before they land in the manuscript.
3. The sandbox is the highest-risk component in the whole system (BRD risk row 6 is the only "High impact" entry that's not already mitigated).

### 2.2 The HITL contract for Phase 3

```
synthesize → await_synthesis_approval ──[approve+has_dataset]──→ analyze_propose
                                       ──[approve+no_dataset]──→ draft_section (skip Phase 3)

analyze_propose ─→ await_code_approval ──[approve]──→ analyze_execute
                                        ──[reject]──→ analyze_propose (regen w/ feedback)
                                        ──[override]──→ analyze_execute (with user-edited code)

analyze_execute ─→ await_analysis_approval ──[approve]──→ draft_section
                                            ──[reject]──→ analyze_propose
                                            ──[override]──→ draft_section (with user-edited result)
```

Two interrupts (not one) is non-negotiable: BRD says *code is always shown before execution*. Approving the code is a separate decision from approving the result.

### 2.3 Sandbox design — three tiers (pick one for v0.2 launch)

| Tier | Tech | Isolation strength | Implementation cost | Recommended for |
| --- | --- | --- | --- | --- |
| **T1: Subprocess + resource limits** | `subprocess.run` with `resource.setrlimit` (CPU/MEM/files) + `restrict_network` socket policy | Weak — kernel-level escape still possible | ~1 sprint | **Dev/staging only.** Matches BRD §7 ("Subprocess + resource limits in v1"). |
| **T2: Docker container (per-call)** | Spawn a `python:3.11-alpine` container with `--network=none --read-only --memory=512m --cpus=1 --pids-limit=64` + bind-mount a scratch dir for figures | Strong — namespace isolation | ~2 sprints | **v0.2 launch target.** |
| **T3: gVisor / Firecracker** | User-space kernel (gVisor) or microVM (Firecracker) | Strongest — syscall filtering / hardware-virt | ~4 sprints + ops | **v1.0 production hardening (BRD §7 v2).** |

**My recommendation: ship T1 in dev only, then T2 (Docker per-call) as the v0.2 launch.** T3 stays on the roadmap for v1.0.

### 2.4 New backend surface — additive, no breaking changes

#### 2.4.1 New endpoints (SPEC §3 amendments — v0.3 of SPEC)

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/projects/{id}/dataset/upload` | Multipart upload of CSV/JSON/Parquet. Stores under per-project namespace in object storage (or local mount in dev). Returns `Dataset` (id, filename, sha256, columns, rowcount). |
| `GET` | `/projects/{id}/datasets` | List uploaded datasets. |
| `DELETE` | `/projects/{id}/datasets/{dataset_id}` | Remove (only allowed before Phase 3 starts). |
| `POST` | `/projects/{id}/workflow/analysis/propose` | (Service-internal — invoked by graph.) Trigger Analyst code-gen. |
| `POST` | `/projects/{id}/workflow/analysis/approve-code` | HITL approve: greenlight code execution. Payload: `{feedback?, override_code?}`. |
| `POST` | `/projects/{id}/workflow/analysis/approve-results` | HITL approve: accept results into manuscript context. Payload: `{feedback?}`. |
| `GET` | `/projects/{id}/artifacts?kind=code\|figure\|log` | (Already exists.) Returns generated code, figures, stdout log. |

The existing `/workflow/approve` and `/workflow/reject` could absorb these via phase-aware routing, but **two separate endpoints (`approve-code`, `approve-results`) are clearer in the audit log** — each row's `action` field names the exact decision the human made.

#### 2.4.2 New Pydantic models

```python
class Dataset(BaseModel):
    id: UUID
    project_id: UUID
    filename: str
    sha256: str
    columns: list[str]
    rowcount: int
    bytes: int
    uploaded_at: datetime

class AnalystInput(BaseModel):           # already exists, fill it out
    task_description: str
    dataset_refs: list[UUID]              # Dataset.id refs
    feedback: str | None = None
    prior_code: str | None = None         # for regenerate-with-feedback path

class AnalystOutput(BaseModel):           # already exists, fill it out
    code: Artifact                        # kind="code", produced_by="analyst"
    figures: list[Artifact]               # kind="figure", produced_by="analyst"
    log: Artifact                         # kind="log", produced_by="analyst" — captures stdout/stderr + exit code
    methods_narrative: str                # short prose for the Scribe to pull into the Methodology section
```

`Artifact.kind` already supports `code`, `figure`, `log` per SPEC §2.2. No DB schema change required for artifacts.

#### 2.4.3 New DB table

```sql
CREATE TABLE datasets (
  id            UUID PRIMARY KEY,
  project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  filename      TEXT NOT NULL,
  sha256        TEXT NOT NULL,
  storage_uri   TEXT NOT NULL,           -- s3://… in prod, file://… in dev
  columns       JSONB NOT NULL,
  rowcount      INTEGER NOT NULL,
  bytes         BIGINT NOT NULL,
  uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_datasets_project ON datasets(project_id);
```

One alembic migration (number `0008`). Add a corresponding `DatasetRow` to `app/models/db.py`.

#### 2.4.4 New graph nodes (LangGraph)

In `app/graph/workflow.py`:

```python
NODE_ANALYZE_PROPOSE = "analyze_propose"          # already declared as NODE_ANALYZE, repurpose
NODE_AWAIT_CODE = "await_code_approval"
NODE_ANALYZE_EXECUTE = "analyze_execute"
NODE_AWAIT_ANALYSIS = "await_analysis_approval"   # already declared, wire it
```

Edges:
```
await_synthesis_approval ─[has_dataset]→ analyze_propose
                          └─[no_dataset]→ draft_section (existing)

analyze_propose → await_code_approval
await_code_approval ─[approve]→ analyze_execute
                    ─[reject]→ analyze_propose (regen)

analyze_execute → await_analysis_approval
await_analysis_approval ─[approve]→ draft_section
                        ─[reject]→ analyze_propose
```

The `has_dataset` predicate reads `len(state.get("datasets") or [])`.

#### 2.4.5 The sandbox (T2 — Docker)

New module `app/services/sandbox.py`:

```python
async def run_in_sandbox(
    code: str,
    datasets: dict[str, bytes],          # filename → bytes (read-only mount)
    timeout_s: int = 60,
    memory_mb: int = 512,
    cpus: float = 1.0,
) -> SandboxResult:
    """Spawn a Docker container, copy code + datasets in, capture artifacts."""
```

Container image: `researchflow-analyst:0.2` — `FROM python:3.11-alpine` + `numpy pandas matplotlib seaborn scipy scikit-learn`. Hardening:
- `--network=none` (no exfil)
- `--read-only` + tmpfs at `/work` for figure writes
- `--memory=512m --memory-swap=512m` (kill on OOM, no swap)
- `--cpus=1.0 --pids-limit=64`
- `--user 65534:65534` (nobody)
- `--cap-drop=ALL --security-opt=no-new-privileges`
- `seccomp` profile drops `unshare`, `clone(CLONE_NEWUSER)`, `ptrace`
- 60-second hard wall-clock timeout (kill on exceed)
- Code is written to `/work/run.py` as the entrypoint; figures land in `/work/figures/`; stdout/stderr captured

Result type:
```python
class SandboxResult(BaseModel):
    exit_code: int
    stdout: str             # cap at 64 KiB
    stderr: str             # cap at 64 KiB
    figures: list[bytes]    # PNG bytes per matplotlib savefig
    duration_ms: int
    timed_out: bool
    oomed: bool
```

### 2.5 Frontend additions

- **Dataset uploader** in the project setup screen (between create-project and Phase 1 start). Drag-and-drop CSV/JSON/Parquet, server-validates the upload, shows columns/rowcount.
- **Code review tab** in a new `AnalysisReview.tsx` component — read-only Python syntax-highlighted view of the generated code + an "Edit & override" affordance that opens a code editor. Reuses the diffLines util.
- **Results preview** — figure carousel + collapsible stdout/stderr + a one-paragraph methods narrative from the Analyst.

### 2.6 Security review checklist (BRD §10 mandate)

Before Phase 3 ships, this checklist runs:

1. ☐ Sandbox container starts with `--network=none --read-only --cap-drop=ALL` — verified by integration test that asserts the container has no outbound socket.
2. ☐ Resource limits enforced — test: a script that allocates 2 GiB triggers OOM kill, not host swap.
3. ☐ Timeout enforced — test: an infinite loop is killed at 60s.
4. ☐ Filesystem isolation — test: code attempting `open("/etc/passwd")` returns errno 13 (permission denied) under the seccomp + read-only mount.
5. ☐ Output capture — stdout/stderr never crash the API on multi-MB output (must truncate at 64 KiB).
6. ☐ Audit log records `analysis.code_proposed`, `analysis.code_approved`, `analysis.code_executed`, `analysis.results_approved` rows with code hash + sandbox exit code.
7. ☐ Frontend always renders the code BEFORE the approve button (no auto-execute path; the BRD invariant).
8. ☐ `pip-audit` clean on the sandbox image. `trivy fs --severity HIGH,CRITICAL` clean.

### 2.7 Roadmap

| Sprint | Deliverable | Owner |
| --- | --- | --- |
| 1 | Dataset upload (FE + BE + alembic 0008 + `DatasetRow`); 501 path for /papers/upload removed; basic listing UI. | Backend + Frontend |
| 2 | `Analyst` agent prompt + structured output (`code` + `methods_narrative`); LLM-only smoke (no sandbox yet) returning code as text. | Backend |
| 3 | Sandbox T2: Docker integration, `services/sandbox.py`, hardening flags, integration tests against the security checklist (§2.6 items 1-5). | Backend + Ops |
| 4 | Graph wiring: 2 new HITL gates, conditional `has_dataset` edge, audit-log actions. New `/workflow/analysis/approve-{code,results}` routes. | Backend |
| 5 | Frontend: `AnalysisReview.tsx` (code view + override + figure carousel + log preview); WS events for `analysis.started`, `analysis.code_ready`, `analysis.results_ready`. | Frontend |
| 6 | Security review gate (§2.6 checklist run in CI as a required check); SPEC v0.3 reflecting the new contract; release notes. | DX + all |

**Estimated total:** 6 sprints, in the v0.2 (Q4 2026) window per BRD §12.

### 2.8 Risks specific to Phase 3 implementation

| Risk | Severity | Mitigation |
| --- | --- | --- |
| LLM generates code that imports `os.system` / `subprocess` / `socket` | HIGH | Static AST scan in `analyze_propose` rejects any code that imports a denylist (`os`, `subprocess`, `socket`, `ctypes`, `requests`, `urllib`); user can still override but the override is loud in the audit log. |
| Sandbox escape via container CVE | HIGH | T2 docker isn't enough on a multi-tenant prod node. v0.2 deployment must run Phase 3 on its own host pool; v1.0 promotes to gVisor (T3). |
| Dataset exfiltration via figure metadata | MEDIUM | Strip EXIF + matplotlib metadata before serving figures back. PNG strip is one line of `Pillow`. |
| OOM on host from runaway code | MEDIUM | `--memory=512m --memory-swap=512m` hard cap. |
| Generated code references a column the dataset doesn't have | LOW | The Analyst's prompt includes the dataset schema (column list) extracted at upload time. Static check before sandbox run. |
| Container start cost (~500ms) makes UX feel slow | LOW | Pre-warm a pool of 2 idle containers per node; rotate on use. v0.3 polish. |

### 2.9 Decision tree — what to start *first*

Before any code lands for Phase 3, decide:

1. **Q: Are we shipping Phase 3 in v0.2 (Q4 2026) or punting again?** If punting, the audit-branch state is fine for v0.1.0 release; no work needed.
2. **Q: T1 (subprocess) or T2 (Docker) for the v0.2 launch?** My recommendation is T2 — the BRD's "subprocess + rlimit" footnote in §7 is the minimum bar, but T2 is roughly the same effort once you've already containerized everything (which this stack is). The marginal isolation strength is large.
3. **Q: Do we need browser-use (FR-1.3) before Phase 3?** No — Phase 3 reads from uploaded datasets, not from browser-scraped sources. Browser-use is the v0.3 work and is independent.
4. **Q: Multi-tenancy in Phase 3?** v0.2 is single-user per BRD §8, so the sandbox host can be shared. The host-pool isolation requirement upgrades to v0.3 + multi-project.

---

## Conclusion

**Direction:** ✅ on track. Build is fully compliant with BRD v0.1; the audit closure metrics validate the engineering posture (0/0/0 bandit, 321 tests, CI gates green). Nothing to course-correct on.

**Phase 3:** ⛔ correctly deferred (BRD-mandated). When you're ready to start v0.2, follow §2.4 → §2.7 of this doc; the security checklist in §2.6 is the merge gate before Phase 3 goes live.

**Recommended next action:** decide on Phase 3 start date. If "now": Sprint 1 (dataset upload + `DatasetRow`) is the cheapest first deliverable and unblocks every later sprint without touching the existing workflow.
