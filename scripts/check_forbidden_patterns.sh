#!/usr/bin/env bash
# Forbidden-pattern grep (M4-A).
#
# Catches regressions of project-wide invariants enforced by prior hardening
# rounds. Each rule below is here because we already shipped a fix for that
# pattern and don't want it sneaking back via PR review oversight.
#
# Rules (each MUST stay at zero matches):
#   1. No hardcoded hex colours in frontend components/app.
#      Why: Tailwind v4 OKLCH palette (frontend/app/globals.css @theme block)
#      is the single source of truth. Round of UI overhaul scrubbed all
#      ``bg-[#xxxxxx]``/``border-[#xxxxxx]``/``text-[#xxxxxx]`` arbitrary
#      colours; a single leak undoes that work.
#
#   2. No raw ``session.commit()`` inside ``backend/app/services``.
#      Why: round-4 MED-2 (workflow.py auto-commit refactor). Services use
#      ``flush_for_background_dispatch`` when they must persist before
#      handing off to a background task; routes rely on the session context
#      manager. A bare commit short-circuits the audit-log + cleanup hooks.
#
#   3. No raw SQL string interpolation in ``backend/app``.
#      Why: SQLAlchemy text() with bound parameters is the only safe path.
#      ``conn.execute(f"SELECT ... {value}")`` is SQL injection waiting to
#      happen.
#
#   4. No ``subprocess`` imports inside ``backend/app`` outside the
#      Phase 3 Analyst sandbox stub (which doesn't exist yet at v0.1).
#      Why: the BRD analyst sandbox is the only legitimate child-process
#      surface; anywhere else is an unaudited code-execution risk.
#
# Exit codes:
#   0 — all clean
#   1 — at least one rule matched (CI fails)
#   2 — script invoked incorrectly

set -euo pipefail

cd "$(dirname "$0")/.."

declare -i violations=0
declare -a failures=()

run_rule() {
    local label="$1"
    local pattern="$2"
    local path="$3"
    local glob="${4:-}"

    local cmd=(grep -rE "$pattern" "$path")
    if [[ -n "$glob" ]]; then
        cmd+=(--include="$glob")
    fi

    # Exclude noise (node_modules, .next, __pycache__, .venv).
    cmd+=(--exclude-dir=node_modules --exclude-dir=.next --exclude-dir=__pycache__ --exclude-dir=.venv --exclude-dir=.git)

    if "${cmd[@]}" >/tmp/forbidden_match.$$ 2>/dev/null; then
        echo ""
        echo "FORBIDDEN PATTERN: $label"
        echo "  pattern: $pattern"
        echo "  path:    $path${glob:+ (filter: $glob)}"
        echo ""
        head -20 /tmp/forbidden_match.$$ | sed 's/^/    /'
        local hits
        hits=$(wc -l < /tmp/forbidden_match.$$ | tr -d ' ')
        if (( hits > 20 )); then
            echo "    ... ($((hits - 20)) more matches)"
        fi
        failures+=("$label")
        violations=$((violations + 1))
    fi
    rm -f /tmp/forbidden_match.$$
}

# Rule 1 — frontend hex colours.
run_rule \
    "hardcoded hex colour in Tailwind class (use @theme tokens)" \
    'bg-\[#|border-\[#|text-\[#' \
    "frontend" \
    "*.tsx"

# Rule 2 — bare session.commit() in services.
# We allow ``await session.commit()`` only inside flush_for_background_dispatch
# itself (backend/app/db/session.py), so scope to services/.
run_rule \
    "bare session.commit() in backend/app/services (use flush_for_background_dispatch)" \
    '^\s*await\s+session\.commit\(\)' \
    "backend/app/services" \
    "*.py"

# Rule 3 — f-string SQL interpolation. Looks for any execute() call whose
# argument is an f-string. The regex is intentionally narrow to avoid false
# positives on benign f-strings in log calls.
run_rule \
    "SQL string interpolation (use SQLAlchemy text() with bind params)" \
    '\.execute\(\s*f"' \
    "backend/app" \
    "*.py"

# Rule 4 — subprocess in app. v0.1 has no analyst sandbox; any subprocess
# is unexpected.
run_rule \
    "subprocess in backend/app (no v0.1 analyst sandbox yet)" \
    '^import subprocess|^from subprocess' \
    "backend/app" \
    "*.py"

if (( violations > 0 )); then
    echo ""
    echo "================================================================"
    echo "$violations forbidden-pattern rule(s) failed:"
    for f in "${failures[@]}"; do
        echo "  - $f"
    done
    echo ""
    echo "Each of these patterns was zeroed-out by a prior hardening round."
    echo "Re-introducing one is a regression. Fix at the source rather than"
    echo "loosening this script."
    echo "================================================================"
    exit 1
fi

echo "scripts/check_forbidden_patterns.sh: all rules clean"
exit 0
