# Compatibility Matrix & Hardening Baseline

**Status:** authoritative for CI and local dev. Update on any intentional
dependency upgrade (regenerate the lock, re-run the gate, commit together).

This document is the "Definition of Done" gate the external-review Action Board
asks for: a committed compatibility matrix + the checklist new PRs cannot bypass.

---

## 1. Verified version matrix (backend)

The full pinned closure lives in [`backend/requirements-lock.txt`](../backend/requirements-lock.txt).
The compatibility-sensitive set — the packages whose drift previously broke
`pytest` at *collection* time (langchain-core ↔ pydantic symbol mismatch) — is:

| Package | Verified version | pyproject bound | Why bounded |
| --- | --- | --- | --- |
| Python | 3.11–3.13 | `requires-python = ">=3.11"` | 3.11 floor (TaskGroup/StrEnum); 3.13 used in dev venv |
| pydantic | 2.13.4 | `>=2.11,<3` | langchain-core imports symbols a too-old/too-new pydantic drops |
| pydantic-settings | 2.14.1 | `>=2.1` | tracks pydantic 2.x |
| langchain-core | 1.4.0 | `>=1.0,<2` | the actual symbol source behind the drift |
| langchain | 1.3.1 | `>=1.0,<2` | tracks langchain-core 1.x |
| langgraph | 1.2.1 | `>=1.0,<2` | interrupt()/checkpoint API stability |
| langgraph-checkpoint-postgres | 3.1.0 | `>=0.1` | Postgres saver |
| pytest-asyncio | 1.3.0 | (dev) | async test collection |
| respx | 0.23.1 | (dev) | httpx mocking |
| email-validator | 2.x | `>=2.0` | EmailStr validation |
| thefuzz | 0.x | `>=0.22` | dedup fuzzy-match |

The preflight (`backend/scripts/preflight.py`) asserts the floors for the
compat trio and reports **VERSION DRIFT** distinctly from MISSING modules.

## 2. The ONE reproducible install path

```bash
cd backend
python -m venv .venv && source .venv/Scripts/activate   # POSIX: .venv/bin/activate
pip install -r requirements-lock.txt        # exact pinned closure (no drift)
pip install -e ".[dev]" --no-deps           # editable install of THIS package only
python scripts/preflight.py                 # interpreter + deps + version-drift check
```

CI (`.github/workflows/backend-ci.yml`) uses exactly this path, then runs
preflight before pytest. `run_ci_local.sh` auto-activates `.venv` and redirects
all caches to a gitignored `.cache/` dir.

---

## 3. Hardening-baseline checklist (Definition of Done)

A PR meets the baseline when **all** of these are green. CI enforces them; they
must be required checks on `main` (see §4).

### Backend
- [ ] `python scripts/preflight.py` — interpreter + 28 required modules + compat trio aligned.
- [ ] `pytest --collect-only` succeeds (no collection-time import errors).
- [ ] `pytest -q` — full suite green (currently **256**: 209 baseline + 47 hardening/contract/security).
- [ ] `ruff check .` and `ruff format --check .` clean.
- [ ] `mypy app` clean (strict).

### Invariant / contract suites (must stay green)
- [ ] `tests/test_workflow_contract.py` — gate transition matrix + GraphState shape + unknown-resume→reject.
- [ ] `tests/test_run_graph_race.py` — transient (CancelledError propagates) vs fatal (run→error).
- [ ] `tests/test_security_regression.py` — dev-bypass refusal, WS rate-limit, body-size 413, phase_locked 409, unified identity, auth-outcomes, no-token-in-logs.

### Frontend
- [ ] `tsc --noEmit` clean.
- [ ] `next lint` clean (only the allow-listed `no-page-custom-font`).
- [ ] `vitest run` green — `lib/api.test.ts` (error typing), `lib/ws.test.ts` (reconnect policy).

---

## 4. Required checks (cannot be bypassed)

These GitHub branch-protection settings on `main` make the baseline
non-bypassable (configure in repo Settings → Branches):

- Require status checks to pass before merging:
  - `backend-ci / lint-and-test`
  - `frontend-ci / <job>`
- Require branches to be up to date before merging.
- Do not allow bypassing the above (including admins).
- Require a pull request before merging (no direct pushes to `main`).

> Husky/lint-staged + the secret-scan + forbidden-pattern scanners run locally
> on commit, but local hooks can be skipped with `--no-verify`; the required
> CI checks above are the authoritative gate.

---

## 5. Exception-handling contract (why broad catches remain)

The remaining `except Exception` blocks are **structurally justified**, not
oversights:

- **Third-party-SDK calls** (LLM gateways, pypdf): these raise wide,
  version-unstable error families; enumerating them would be more brittle than
  a broad catch. Each logs `error_type=type(exc).__name__` so the *defect class*
  is queryable.
- **Background-task boundaries** (`_run_graph`, `_resume_graph`): a crash would
  silently kill a worker, so they catch broadly — but **always re-raise
  `asyncio.CancelledError`** first (shutdown is control-flow, not failure) and
  emit a structured `error_code` for everything else. Locked by
  `test_run_graph_race.py`.
- **Best-effort enrichment** (unpaywall, fulltext) and **per-query isolation**
  (discovery router): one item's failure must not sink the batch.

Inner logic with *known* failure modes already uses typed catches
(`unpaywall._lookup_pdf_url` → `httpx.HTTPError` / `(ValueError, TypeError)`).
