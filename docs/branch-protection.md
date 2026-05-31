# Branch Protection — `main`

The merge gate for `main` enforces every check that `run_ci_local.sh` runs locally, plus an approval requirement. Edit this file and re-apply via `gh api` after every CI workflow change so the gate matches.

## Required status checks

These names must match the job names in `.github/workflows/ci.yml`. If a check fails or doesn't report, merge is blocked.

| Check name | What it gates | First introduced |
| --- | --- | --- |
| `preflight` | Python 3.11+, every runtime module importable, compat trio aligned | M1-A |
| `ruff-check` | Lint clean across `backend/app` + `backend/tests` | (always-on) |
| `ruff-format` | Format clean | (always-on) |
| `mypy-strict` | `mypy --strict app/` returns 0 issues | (always-on) |
| `pytest` | Full backend pytest suite passes, coverage ≥ 80% | M4-A |
| `bandit` | 0 HIGH + 0 MEDIUM bandit findings (LOW logged only) | audit/2026-05-31 |
| `radon-budget` | No new function ranks D or worse without a waiver in `.audit/radon-waivers.txt` | audit/2026-05-31 |
| `forbidden-patterns` | No hex-color leaks, raw SQL interpolation, or banned imports | M4-A |
| `secret-scan` | No sk-ant / AIzaSy / s2k- / GitHub PAT patterns in the diff | M1-E |
| `pip-audit` | 0 HIGH/CRITICAL backend dep CVEs (strict mode in CI; warn locally) | M4-A |
| `frontend-tsc` | `npx tsc --noEmit` clean | (always-on) |
| `frontend-lint` | `next lint` returns 0 errors (warnings allowed for pre-existing) | (always-on) |
| `frontend-test` | `vitest run` passes | (always-on) |
| `npm-audit` | `npm audit --omit=dev --audit-level=high` clean | M4-A |
| `workflow-contract` | `tests/test_workflow_contract.py` and `test_hardening_m4_e2e.py` pass | M4-B |
| `security-regression` | `tests/test_security_regression.py` and the four audit-phaseN test files pass | M4-B |

## Reviewer rule

- 1 approving review required.
- Approvals dismissed on new commit (so a reviewer who approved a stale diff can't auto-rubber-stamp).
- "Require review from Code Owners" enabled once `CODEOWNERS` lands.

## Force-push policy

- No force-push to `main`. No deletion of `main`.
- `audit/*` branches: force-push allowed (cherry-picks during a hardening sprint).
- `feature/*` branches: force-push allowed pre-PR-open; locked once a PR exists (to keep review comments anchored).

## How to apply via gh

```powershell
$repo = "owner/repo"   # replace
gh api `
  -X PUT `
  -H "Accept: application/vnd.github+json" `
  "repos/$repo/branches/main/protection" `
  -F required_status_checks.strict=true `
  -F required_status_checks.contexts[]="preflight" `
  -F required_status_checks.contexts[]="ruff-check" `
  -F required_status_checks.contexts[]="ruff-format" `
  -F required_status_checks.contexts[]="mypy-strict" `
  -F required_status_checks.contexts[]="pytest" `
  -F required_status_checks.contexts[]="bandit" `
  -F required_status_checks.contexts[]="radon-budget" `
  -F required_status_checks.contexts[]="forbidden-patterns" `
  -F required_status_checks.contexts[]="secret-scan" `
  -F required_status_checks.contexts[]="pip-audit" `
  -F required_status_checks.contexts[]="frontend-tsc" `
  -F required_status_checks.contexts[]="frontend-lint" `
  -F required_status_checks.contexts[]="frontend-test" `
  -F required_status_checks.contexts[]="npm-audit" `
  -F required_status_checks.contexts[]="workflow-contract" `
  -F required_status_checks.contexts[]="security-regression" `
  -F required_pull_request_reviews.dismiss_stale_reviews=true `
  -F required_pull_request_reviews.required_approving_review_count=1 `
  -F enforce_admins=true `
  -F allow_force_pushes=false `
  -F allow_deletions=false
```

## Verification after apply

```powershell
gh api "repos/$repo/branches/main/protection" --jq '.required_status_checks.contexts'
```

Output should list all 16 contexts above. If any are missing, re-run the apply command.
