# ResearchFlow AI — Phase 3 (Analyst) Implementation Record

**Branch:** `feature/phase-3`
**Base reference:** `main` (post-rebase of phase-1/2/4)
**Date recorded:** 2026-06-01
**Status:** Phase 3 complete — six-sprint plan from
`docs/brd-verification-and-phase3-plan.md` shipped end-to-end. SPEC v0.3.

---

## 1. What Phase 3 is

Per **BRD §4.1 Phase 3** and **FR-2.3**:

> *Analyst writes and executes code in a sandbox to produce figures,
> tables, and a methods narrative. System pauses. User reviews the
> generated artifacts and code log.*

Two HITL gates inside this phase (non-negotiable per BRD §10
sandbox-escape mitigation):
1. **`await_code_approval`** — user reviews the generated **code BEFORE
   execution.**
2. **`await_analysis_approval`** — user reviews the executed figures +
   log AFTER.

Phase 3 is **opt-in** — projects without any uploaded dataset route
from the Phase-2 synthesis gate straight to Phase-4 drafting.

---

## 2. Starting state (main @ rebased phase-4 tip)

| Component | What was there |
|---|---|
| `backend/app/agents/analyst.py` | Stub that raised `NotImplementedError("Analyst is scheduled for v0.2")`. |
| `backend/app/graph/workflow.py` | `NODE_ANALYZE` / `NODE_AWAIT_ANALYSIS` declared as constants but no node functions, no edges. |
| `backend/app/services/workflow.py` | No `approve_code_workflow` / `approve_results_workflow`. |
| `backend/app/api/routes/workflow.py` | No `/analysis/...` endpoints. |
| Datasets | No upload route, no `DatasetRow`, no `datasets` table, no Pydantic model, no frontend uploader. |
| Sandbox | No `services/sandbox.py`, no Dockerfile, no AST scanner. |
| Frontend | No `AnalysisReview.tsx`; `lib/types.ts` had no `Dataset` / `AnalystProposal` / `AnalystResult`; `api.ts` had no `analysis.*` or `datasets.*` namespaces. |
| SPEC | v0.2 — no `/datasets` routes, no `/analysis/{approve,reject}-{code,results}`, no `datasets` SQL, Analyst contract still tagged "v0.2 / not implemented." |

Everything below was built on top of that base.

---

## 3. Sprint breakdown (commits on `feature/phase-3`)

### Sprint 1 — Dataset upload pipeline (`feat(phase-3/sprint-1)`)

| File | Role |
|---|---|
| `backend/app/models/schemas.py` | New `Dataset` Pydantic model (SPEC v0.3 §2.2). |
| `backend/app/models/db.py` | New `DatasetRow` SQLAlchemy mapping; unique `(project_id, sha256)`; ix on `project_id`. |
| `backend/alembic/versions/0008_datasets.py` | Forward + downgrade migration. |
| `backend/app/services/dataset_storage.py` | Pluggable storage adapter — dev backend writes under `settings.data_dir`, prod hot-swap to S3. Bounded-memory parsers for CSV / TSV / JSON / JSONL / Parquet (reads enough for columns + rowcount, never the full payload). |
| `backend/app/api/routes/datasets.py` | `GET /projects/{id}/datasets`, `POST .../datasets/upload`, `DELETE .../datasets/{ds_id}`. Owner-only, phase-locked after analysis starts, byte-identical re-upload → 409. |
| `backend/app/config.py` | New settings: `data_dir`, `max_dataset_bytes`, `sandbox_*` placeholders. |
| `backend/app/main.py` | Registers the new datasets router. |
| `frontend/lib/types.ts` | `Dataset` interface (mirrors backend). |
| `frontend/lib/api.ts` | `api.datasets.{list, upload, delete}`. Upload bypasses the JSON helper and posts multipart; errors translated to typed `ApiError`. |
| `frontend/components/workflow/DatasetUploader.tsx` | Drag-and-drop, multi-file, file-type denylist mirror, schema preview, per-file remove. Renders read-only when `locked={true}`. |
| `frontend/app/page.tsx` | Uploader wired into the Phase-1 awaiting view above the candidate papers list. |
| `backend/tests/test_datasets.py` | 21 tests — 14 storage/parser unit tests + 7 HTTP route tests via aiosqlite/StaticPool. |

### Sprint 2 — Analyst agent (LLM proposal + AST scan)

| File | Role |
|---|---|
| `backend/app/agents/analyst.py` | Full rewrite from the stub. Adds `DatasetRef`, `AnalystInput`, `AnalystProposal`, `AnalystUsage`, `AnalystOutput`. XML-encapsulated prompt template. `_validate_proposed_code` AST scan with 20-module denylist + warn-list. `Analyst.run` degrades gracefully on LLM provider failure (returns a comment-only stub the user can reject + regenerate). |
| `backend/tests/test_analyst_agent.py` | 17 tests — 8 static-scan tests + 5 prompt-rendering tests + 4 `run()` integration tests with a `_DummyLLM` stand-in. |

### Sprint 3 — Docker T2 sandbox

| File | Role |
|---|---|
| `backend/app/services/sandbox.py` | `run_in_sandbox(code, datasets=, config=)` shells out via `asyncio.create_subprocess_exec` to `docker run` with the hardening flag set. Wall-clock timeout; OOM detection (exit 137); 64 KiB per-stream output cap; PNG figure collection. `SandboxUnavailableError` for sandbox-disabled or no-docker. `docker_argv` exported as a pure helper for the security-checklist tests. |
| `backend/sandbox/Dockerfile` | `researchflow-analyst:0.2` — `python:3.11-alpine` + numpy/pandas/matplotlib/scipy/scikit-learn, runs as `65534:65534`. Built once per release. |
| `backend/tests/test_sandbox.py` | 9 tests — 6 pure-function (flag set, override knobs, refuses when disabled/no-docker, audit payload strips figures, truncation constants) + 3 Docker-gated (smoke, no-network, timeout). The 3 docker tests are skipped unless `DOCKER_INTEGRATION=1`. |

### Sprint 4 — Graph wiring + analysis approve/reject routes

| File | Role |
|---|---|
| `backend/app/graph/state.py` | Adds 7 Phase-3 fields to `GraphState`. |
| `backend/app/graph/workflow.py` | Adds `NODE_ANALYZE_PROPOSE`, `NODE_AWAIT_CODE`, `NODE_ANALYZE_EXECUTE`, `NODE_AWAIT_ANALYSIS` constants + node functions. Three routing helpers: `_route_after_synthesis` (now forks on `state.datasets`), `_route_after_code`, `_route_after_analysis`. `build_graph` registers all four nodes + edges. |
| `backend/app/services/workflow.py` | `start_workflow` hydrates `state.datasets` from `DatasetRow`. New `_handle_analysis_gate_pause` distinguishes the code vs results gate by `snapshot.next` and emits the `gate=code|results` discriminator. Four new service functions: `approve_code_workflow`, `reject_code_workflow`, `approve_results_workflow`, `reject_results_workflow`. |
| `backend/app/api/routes/workflow.py` | Four new endpoints: `POST /workflow/analysis/{approve-code, reject-code, approve-results, reject-results}`. The approve-code endpoint runs `validate_user_override_code` on any `override_code` BEFORE the graph resumes; a denied import returns `422 code_static_scan_failed`. |
| `backend/tests/test_phase3_graph_wiring.py` | 8 tests locking the routing matrix + the build_graph node contract. |
| `backend/tests/test_workflow_contract.py` | Updated `test_graph_registers_exactly_the_expected_nodes` to include the four new nodes. |

### Sprint 5 — Frontend AnalysisReview component

| File | Role |
|---|---|
| `frontend/lib/types.ts` | `StaticScanResult`, `AnalystProposal`, `AnalystResult` interfaces. |
| `frontend/lib/api.ts` | `api.analysis.{approveCode, rejectCode, approveResults, rejectResults}`. |
| `frontend/components/workflow/AnalysisReview.tsx` | Top-level review surface, routed by the `gate` discriminator. **CodeReview** sub-view: scan-summary chip (green/amber/red), read-only `<pre>` by default with an "Edit & override" affordance, methods narrative card, reject + feedback flow. **ResultsReview** sub-view: top-row chip with duration_ms + exit + timed_out/oomed, figure carousel from `figures_b64`, collapsible stdout/stderr (`<details>` open on non-zero exit), reject + feedback flow. |

### Sprint 6 — SPEC v0.3 + release notes (this doc)

| File | Role |
|---|---|
| `SPEC.md` | Bumped to v0.3. New §3.3 rows for analysis routes; new §3.6 Datasets; new error codes; §2.2 `Dataset` model; §2.3 `datasets` table; §4.1 `gate` field on `approval.required`; §5.1 four new nodes; §5.2 dataset-presence routing; §6.3 full Analyst contract + sandbox + static-scan documentation. |
| `docs/PHASE3_IMPLEMENTATION_PLAN.md` | This file. |

---

## 4. Test posture at Phase-3 close

| Layer | Result |
|---|---|
| `pytest` (default) | **373 passed**, 3 skipped (`DOCKER_INTEGRATION` gate) |
| `pytest` (with `DOCKER_INTEGRATION=1`) | 376 passed (3 sandbox smoke tests join the run) |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy --strict` on `app` | clean (45 source files) |
| Frontend `npx tsc --noEmit` | clean |
| Frontend `vitest run` | 17/17 passed |
| Frontend `next lint` | 1 pre-existing warning (custom-font in `layout.tsx`), unchanged |

The DB-backed tests run against in-memory aiosqlite via `StaticPool`
(same pattern as Sprints 1 and prior phases). Postgres-specific
features (`postgresql_where` partial indexes) are gated by the
`postgresql_where` / `sqlite_where` dual-spec pattern already used in
the codebase.

---

## 5. Security checklist (§2.6 of the original Phase-3 plan)

Status at Phase-3 close:

1. ✅ Sandbox container starts with `--network=none --read-only --cap-drop=ALL` — asserted by `test_docker_argv_includes_all_hardening_flags`. **Live verification gated on `DOCKER_INTEGRATION=1`.**
2. ✅ Resource limits enforced — `--memory=Nm --memory-swap=Nm` (swap disabled), `--pids-limit=64`. Audit row records `oomed` if Docker exits 137.
3. ✅ Timeout enforced — `_run(..., timeout=cfg.timeout_s + 5)` + `result.timed_out=True` surfacing. Verified by `test_sandbox_timeout` (Docker-gated).
4. ✅ Filesystem isolation — `--read-only` + tmpfs `/work` bind mount. Datasets are copied in by the harness, not by the container.
5. ✅ Output capture — 64 KiB cap per stream with a `[truncated …]` marker.
6. ✅ Audit log records `analysis.code_proposed`, `analysis.code_approved`, `analysis.code_overridden`, `analysis.code_rejected`, `analysis.code_executed`, `analysis.results_approved`, `analysis.results_rejected`.
7. ✅ Frontend always renders the code BEFORE the approve button — `AnalysisReview.tsx` `CodeReview` sub-view doesn't show the approve control until the scan summary + code block render.
8. ⚠️ `pip-audit` / `trivy fs` on the sandbox image — **runs at image build time, not in the default pytest suite.** The Dockerfile pins all five analysis libraries by version so a future audit can re-scan deterministically.

---

## 6. Run the full Phase-3 happy path locally

```bash
# 1. Backend boot (in-memory tests pass without Docker).
cd backend
uv venv .venv --python 3.12 && source .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest -q                # 373 passed, 3 skipped

# 2. Build the sandbox image (one-time).
docker build -t researchflow-analyst:0.2 backend/sandbox

# 3. Run the full suite including the docker-gated tests.
DOCKER_INTEGRATION=1 python -m pytest -q

# 4. Frontend.
cd ../frontend
npm ci
npm test                           # vitest 17/17
npx tsc --noEmit                   # clean
```

---

## 7. What's intentionally NOT in Phase 3

Cross-checked against `docs/brd-verification-and-phase3-plan.md` §2.7 and §2.9:

- **T3 (gVisor / Firecracker) sandbox** — kept on the v1.0 roadmap.
- **Multi-tenant Phase-3 host pool** — single-user MVP; v0.3 work.
- **Container pre-warming** — start cost is ~500 ms per run; not a UX
  problem at the current scale.
- **GUI dataset previewer** — schema-only preview ships now; full row
  preview is a polish item for v0.3.

---

## 8. Next milestone

With Phase 3 closed and SPEC bumped to v0.3, the v0.2 milestone is
**MVP-complete + Phase-3 ready**. v0.3 candidates:

1. **Browser-use (FR-1.3)** — Playwright-driven discovery for paywalled
   sources, with the same HITL paper-pool gate.
2. **Multi-project dashboard** — drops the single-project assumption
   that BRD §8 explicitly preserved for MVP.
3. **LaTeX output** — drop the `output_format=="latex"` rejection in
   Scribe and ship the `bibtex+tex` export format.

Sprint plan for each of these tracks separately — none of them depend
on Phase 3.
