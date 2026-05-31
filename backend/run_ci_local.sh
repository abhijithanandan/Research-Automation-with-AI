#!/usr/bin/env bash
# Run CI checks locally before pushing. Must all pass before opening a PR.
#
# Hardening gate (M4-A): in addition to the always-on lint+type+test loop,
# the script invokes the three project-wide invariant scanners that
# M1/M2/M3/M4 introduced. Each is a defense against a regression class we
# already shipped a fix for.
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(cd .. && pwd)"

# --- Interpreter guard ---------------------------------------------------
# The #1 cause of "27 collection errors" is running this against the system
# Python where the deps were never installed. If a project .venv exists and
# we are not already inside a venv, activate it so the whole pipeline uses
# the SAME interpreter the deps were installed into. Fail loud if neither.
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    if [[ -f ".venv/Scripts/activate" ]]; then
        # shellcheck disable=SC1091
        source ".venv/Scripts/activate"   # Windows (Git Bash / MSYS)
    elif [[ -f ".venv/bin/activate" ]]; then
        # shellcheck disable=SC1091
        source ".venv/bin/activate"        # POSIX
    fi
fi
PY="python"

# --- Writable cache dirs -------------------------------------------------
# Avoids the ".pytest_cache access denied" / pycache permission noise the
# reviewer flagged: pin every cache to a writable, gitignored scratch dir.
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-$PWD/.cache/pycache}"
PYTEST_CACHE="$PWD/.cache/pytest"
RUFF_CACHE="$PWD/.cache/ruff"
MYPY_CACHE="$PWD/.cache/mypy"
mkdir -p "$PYTHONPYCACHEPREFIX" "$PYTEST_CACHE" "$RUFF_CACHE" "$MYPY_CACHE"

echo "==> preflight (interpreter + required deps + version drift)"
"$PY" scripts/preflight.py

echo "==> ruff check"
ruff check --cache-dir "$RUFF_CACHE" .

echo "==> ruff format --check"
ruff format --check --cache-dir "$RUFF_CACHE" .

echo "==> mypy"
mypy --cache-dir "$MYPY_CACHE" app

echo "==> pytest with coverage"
"$PY" -m pytest -p no:cacheprovider -o cache_dir="$PYTEST_CACHE" \
    --cov=app --cov-report=term-missing --cov-report=annotate:cov_annotate -q

echo "==> bandit (audit/2026-05-31 gate)"
# Fail on MEDIUM-or-higher findings. Bandit's CLI returns 0 on filtered runs,
# so we count from the JSON ourselves. LOW noise (asserts) tracked separately
# and addressed in the Wave-3 cleanup batch.
# Use a relative path the way Windows Python expects it — Git Bash's $ROOT
# resolves to /c/Users/... which Windows Python can't open.
BANDIT_REPORT="../reports/bandit.json"
mkdir -p "$(dirname "$BANDIT_REPORT")"
bandit -q -f json -o "$BANDIT_REPORT" -r app || true
"$PY" -u -c "
import json, sys
d = json.load(open('$BANDIT_REPORT'))
m = d.get('metrics', {}).get('_totals', {})
med, hi = m.get('SEVERITY.MEDIUM', 0), m.get('SEVERITY.HIGH', 0)
if med + hi > 0:
    print(f'    bandit gate FAIL: {hi} HIGH + {med} MEDIUM findings — fix or waive in findings-matrix.md')
    sys.exit(1)
print(f'    bandit gate OK: 0 HIGH + 0 MEDIUM ({m.get(\"SEVERITY.LOW\", 0)} LOW logged)')
"

echo "==> radon complexity budget (audit/2026-05-31 gate)"
# Block functions ranked D or worse, except for a waivered set captured in
# .audit/radon-waivers.txt (one entry per "file:function" pair, pre-existing
# D-ranks in discovery adapters + reference formatter).
bash "$ROOT/scripts/check_radon_budget.sh" || { echo "    radon: new function exceeds complexity budget (rank D+). Refactor or add a documented waiver."; exit 1; }

echo "==> forbidden-pattern grep (M4-A)"
bash "$ROOT/scripts/check_forbidden_patterns.sh"

echo "==> secret scan over working tree (M1-E / M4-A)"
bash "$ROOT/scripts/check_secrets.sh" --all

# pip-audit and npm audit are deliberately non-fatal here when the
# dependency network isn't reachable (offline dev). The hardening report
# documents the policy: HIGH/CRITICAL findings fail the gate in CI; local
# dev gets a warning so the developer is aware but isn't blocked.
echo "==> pip-audit (M4-A)"
if command -v pip-audit >/dev/null 2>&1; then
    # --strict makes vulnerability lookup failures non-zero; we wrap in `|| true`
    # because dev machines may be offline. CI flips this back to strict via
    # `PIP_AUDIT_STRICT=1`.
    if [[ "${PIP_AUDIT_STRICT:-0}" == "1" ]]; then
        pip-audit --strict --requirement <(pip freeze)
    else
        pip-audit --requirement <(pip freeze) || echo "    pip-audit non-zero (treat as warning in local dev)"
    fi
else
    echo "    pip-audit not installed; run 'pip install -e .[dev]' to enable"
fi

echo "==> npm audit (M4-A)"
# Wave-1 closure accepted 4 residual HIGH advisories on next@14.2.35 as
# non-exploitable in this deployment (image-optimiser, RSC HTTP-deserialise,
# rewrites smuggling, postcss CSS-stringify XSS — features not used). The
# gate blocks on CRITICAL only; the local warn still surfaces HIGH so a real
# patch landing isn't invisible. When the deferred Next 15.x bump lands,
# switch this back to --audit-level=high.
NPM_AUDIT_CI_LEVEL="critical"
NPM_AUDIT_LOCAL_LEVEL="high"
if [[ -d "$ROOT/frontend/node_modules" ]]; then
    if [[ "${CI:-0}" == "1" ]]; then
        (cd "$ROOT/frontend" && npm audit --omit=dev --audit-level="$NPM_AUDIT_CI_LEVEL")
    else
        (cd "$ROOT/frontend" && npm audit --omit=dev --audit-level="$NPM_AUDIT_LOCAL_LEVEL") \
            || echo "    npm audit found HIGH (accepted residuals; see Wave-1 closure) — review for new entries"
    fi
elif command -v docker >/dev/null 2>&1 \
     && docker compose -f "$ROOT/docker-compose.yml" ps frontend 2>/dev/null | grep -q Up; then
    if [[ "${CI:-0}" == "1" ]]; then
        docker compose -f "$ROOT/docker-compose.yml" exec -T frontend \
            sh -c "npm audit --omit=dev --audit-level=$NPM_AUDIT_CI_LEVEL"
    else
        docker compose -f "$ROOT/docker-compose.yml" exec -T frontend \
            sh -c "npm audit --omit=dev --audit-level=$NPM_AUDIT_LOCAL_LEVEL" \
            || echo "    npm audit found HIGH (accepted residuals; see Wave-1 closure) — review for new entries"
    fi
else
    echo "    frontend/node_modules absent and container not up; skip"
fi

# --- Frontend type + lint gates (W2-D1) ------------------------------------
# Always-on contract: tsc strict + next lint clean. Runs against the running
# frontend container (where node_modules live). Skipped only if neither host
# node_modules nor a live container exists.
echo "==> frontend tsc --noEmit (W2-D1)"
if [[ -d "$ROOT/frontend/node_modules" ]]; then
    (cd "$ROOT/frontend" && npx tsc --noEmit)
elif command -v docker >/dev/null 2>&1 \
     && docker compose -f "$ROOT/docker-compose.yml" ps frontend 2>/dev/null | grep -q Up; then
    docker compose -f "$ROOT/docker-compose.yml" exec -T frontend \
        sh -c "npx tsc --noEmit"
else
    echo "    frontend not available; skip"
fi

echo "==> frontend next lint (W2-D1)"
if [[ -d "$ROOT/frontend/node_modules" ]]; then
    (cd "$ROOT/frontend" && npx next lint)
elif command -v docker >/dev/null 2>&1 \
     && docker compose -f "$ROOT/docker-compose.yml" ps frontend 2>/dev/null | grep -q Up; then
    docker compose -f "$ROOT/docker-compose.yml" exec -T frontend \
        sh -c "npx next lint"
else
    echo "    frontend not available; skip"
fi

echo ""
echo "All checks passed."
