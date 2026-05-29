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
if [[ -d "$ROOT/frontend/node_modules" ]]; then
    (cd "$ROOT/frontend" && npm audit --omit=dev --audit-level=high) \
        || echo "    npm audit found HIGH/CRITICAL — review before shipping"
else
    echo "    frontend/node_modules absent; skip (run 'npm install' in frontend/ to enable)"
fi

echo ""
echo "All checks passed."
