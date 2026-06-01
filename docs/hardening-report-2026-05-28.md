# ResearchFlow AI — Hardening Report 1

**Program:** Complete Codebase Hardening (M1–M4)
**Branch:** `feature/phase-4`
**Tip commit:** `ac1d727`
**Date:** 2026-05-28
**Status:** Hardened — all M4 verification gates green.

---

## 1. Scope

A proactive security/reliability/correctness sweep run as four independently
shippable milestones on top of the Phase-2 baseline. The architecture was
left intact (FastAPI + LangGraph + Postgres + Chroma + Next.js); this program
tightened the existing surface rather than re-platforming.

The work builds on the Phase-2 PR #5 review fixes (commit `899345e`): reject
persistence, single source of truth for the candidate pool, and a functional
per-project cost cap (NFR-5). Those fixes are a prerequisite of this branch.

---

## 2. Verification gate (M4) — current state

| Check | Result |
| --- | --- |
| `ruff check` | clean |
| `ruff format --check` | clean (78 files) |
| `mypy --strict app/` | clean (39 source files) |
| `pytest` | **209 passed** |
| Forbidden-pattern grep (`scripts/check_forbidden_patterns.sh`) | clean |
| Secret scan (`scripts/check_secrets.sh --all`) | clean |

Hardening test inventory:

| Suite | Tests |
| --- | --- |
| `test_hardening_m1.py` | 8 |
| `test_hardening_m2.py` | 6 |
| `test_hardening_m3.py` | 11 |
| `test_hardening_m4_e2e.py` | 2 |
| `test_cost_cap.py` | 11 |

---

## 3. Findings & fixes by milestone

### M1 — Security & environment gate

| ID | Severity | Area | Fix | File(s) |
| --- | --- | --- | --- | --- |
| M1-A | High | Build/test reproducibility | Pin Python ≥3.11; preflight verifies every runtime + dev import before pytest collection; `run_ci_local.sh` fails fast on preflight | `backend/scripts/preflight.py`, `backend/run_ci_local.sh`, `backend/pyproject.toml` |
| M1-B | High | Auth-bypass guard | Boot refuses `DEV_AUTH_BYPASS=true` outside `APP_ENV=development`; structured `app.start` posture log on every boot | `backend/app/main.py` |
| M1-C | Med | DoS surface | Per-actor sliding-window rate limits (projects create 30/min, workflow start 10/min, paper patch/delete 60/min); body-size cap middleware (1 MiB JSON / 50 MiB upload) returns 413 | `backend/app/api/rate_limit.py`, `backend/app/api/middleware.py`, route deps |
| M1-D | Med | Log redaction | Auth/WS paths log `error_type` + structured `event/actor/result/reason_code`, never the JWT; removed `exc_info` that captured tokens in stack frames | `backend/app/api/deps.py`, `backend/app/api/routes/websocket.py`, `backend/app/services/auth.py` |
| M1-E | Med | Secret leakage | Pre-commit secret scanner (`sk-ant-`, `AIzaSy`, `s2k-`, `sk-`, `xoxb-`, `ghp_`, `ghs_`) wired into Husky and CI | `scripts/check_secrets.sh`, `.husky/pre-commit` |

### M2 — Data integrity & state-machine correctness

| ID | Severity | Area | Fix | File(s) |
| --- | --- | --- | --- | --- |
| M2-A | High | Double-approval race | Partial unique index `uq_audit_pool_approval_per_run` on `audit_log(workflow_run_id) WHERE action='phase_1.approved_pool'`; `approve_workflow` translates `IntegrityError` to 409 `already_approved` | `backend/alembic/versions/0006_audit_pool_approval_unique.py`, `backend/app/models/db.py`, `backend/app/services/workflow.py` |
| M2-B | Med | Conflict-safe persistence | Confirmed all hot inserts use `ON CONFLICT DO NOTHING` / unique-PK shapes; no remaining check-then-insert | `backend/app/services/workflow.py` |
| M2-C | Med | Atomic transitions | `(state, phase)` always move together in a single `_update_run_state` statement; asserted by source-level guard test | `backend/app/services/workflow.py` |
| M2-D | Med | Phase-lock via audit marker | Generalized `_assert_run_in_state(session, run_id, expected_states)`; pool-lock derives from the immutable `phase_1.approved_pool` audit row, not run-state strings | `backend/app/services/workflow.py`, `backend/app/api/routes/papers.py` |

### M3 — Reliability, external I/O, frontend resilience

| ID | Severity | Area | Fix | File(s) |
| --- | --- | --- | --- | --- |
| M3-A | Med | Transaction model | All routes commit-free (session-context auto-commit); `CancelledError` re-raised in every long-running task; broad catches emit structured `error_code` | `backend/app/services/workflow.py` |
| M3-B | Med | Adapter robustness | Vector-store URL scheme whitelist (`http`/`https` only) via `urllib.parse`; fulltext fetcher rejects `text/html` 200s by Content-Type before reading body (defense-in-depth with `%PDF` magic sniff); all 5 discovery adapters follow `_search_with_retry` + `RetryError` + `_safe_json` | `backend/app/services/vector_store.py`, `backend/app/services/fulltext_fetcher.py`, `backend/app/services/discovery.py` |
| M3-C | Med | Frontend resilience | WS reconnect allow-list keeps 4429 (retry), excludes 4401/4403 (give up); `ApiError` taxonomy categorizes 401/403/409/422/5xx; phase-conflict banner | `frontend/lib/ws.ts`, `frontend/lib/api.ts`, `frontend/app/page.tsx` |

### M4 — Verification & static-analysis gate

| ID | Severity | Area | Fix | File(s) |
| --- | --- | --- | --- | --- |
| M4-A | — | CI baseline | `run_ci_local.sh` runs preflight → ruff → format → mypy → pytest → forbidden-pattern grep → secret scan → pip-audit → npm audit | `backend/run_ci_local.sh`, `scripts/check_forbidden_patterns.sh`, `backend/pyproject.toml` (adds `pip-audit`) |
| M4-B | — | E2E regression | `test_hardening_m4_e2e.py` drives a full HITL workflow (approve P1 → P2 → 7 drafting approvals → manuscript) asserting the audit trail + state-machine invariants; double-approve blocked by the M2-A index | `backend/tests/test_hardening_m4_e2e.py` |
| M4-C | — | This report | Published | `docs/hardening-report-2026-05-28.md` |

---

## 4. Forbidden-pattern invariants (regression guards)

`scripts/check_forbidden_patterns.sh` fails CI on any of:

1. Hardcoded hex colours in `frontend/**/*.tsx` (Tailwind v4 OKLCH `@theme` is the single source).
2. Bare `await session.commit()` in `backend/app/services` (must use `flush_for_background_dispatch`).
3. f-string SQL interpolation in `backend/app` (must use SQLAlchemy `text()` with bound params).
4. `subprocess` imports in `backend/app` (no analyst sandbox at v0.1).

All four are currently at zero matches.

---

## 5. Known-deferred items

- **Phase 3 Analyst sandbox** — BRD v0.2; needs its own security review (sandboxed code execution). Out of scope here.
- **`pip-audit` / `npm audit`** — wired into the CI script but non-fatal in offline local dev; CI flips `pip-audit` to strict via `PIP_AUDIT_STRICT=1`.
- **Full historical secret rewrite** (`git filter-repo`) — out of scope; key rotation is the mitigation. No live keys are committed (`.env` is gitignored).
- **Multi-tenancy / org-level RBAC** — BRD non-goal at v0.1.

---

## 6. Day-0 prerequisite (operator action)

Every API key that appeared in a chat transcript or `.env` during development
must be rotated, with per-provider billing caps set so any future leak has a
bounded blast radius. This is an operator action outside the codebase and is
not verifiable from the repository.
