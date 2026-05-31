#!/usr/bin/env bash
# Radon complexity gate — audit/2026-05-31.
#
# Policy:
#   - New or modified function/method ranked D or worse → fail.
#   - Existing rank-D functions get a documented waiver in
#     .audit/radon-waivers.txt. Each line: "file:function" (no spaces).
#
# Rationale: radon's rank D = cyclomatic complexity 21–30. The existing
# discovery adapters and one reference formatter sit there because they
# parse heterogeneous external API shapes — the complexity is essential,
# not accidental. Anything new that lands at D needs an explicit waiver
# entry with a 1-line justification in the matrix.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WAIVERS="$ROOT/.audit/radon-waivers.txt"
APP_DIR="$ROOT/backend/app"

# Find radon in the active venv or PATH.
if command -v radon >/dev/null 2>&1; then
    RADON="radon"
elif [[ -x "$ROOT/backend/.venv/Scripts/radon.exe" ]]; then
    RADON="$ROOT/backend/.venv/Scripts/radon.exe"
elif [[ -x "$ROOT/backend/.venv/bin/radon" ]]; then
    RADON="$ROOT/backend/.venv/bin/radon"
else
    echo "radon not installed; run 'pip install radon' or activate the venv"
    exit 0   # advisory, not gating, when the tool itself is missing
fi

# radon prints "    F 209:0 _deduplicate - D (24)" style lines. Capture only
# rank D/E/F entries — these are the new-finding candidates.
mapfile -t offenders < <(
    "$RADON" cc "$APP_DIR" -s 2>/dev/null \
        | grep -E " - [DEF] \(" \
        | sed -E 's/^[[:space:]]+[CFM][[:space:]]+[0-9]+:[0-9]+[[:space:]]+([^ ]+)[[:space:]]+-[[:space:]]+[DEF].*/\1/'
)

# Load waivers (function names, one per line, # comments allowed).
declare -A WAIVED=()
if [[ -f "$WAIVERS" ]]; then
    while IFS= read -r line; do
        # strip comments + whitespace
        line="${line%%#*}"
        line="${line//[[:space:]]/}"
        [[ -z "$line" ]] && continue
        WAIVED["$line"]=1
    done < "$WAIVERS"
fi

# Diff: any offender NOT on the waiver list is a fail.
new_offenders=()
for o in "${offenders[@]}"; do
    if [[ -z "${WAIVED[$o]:-}" ]]; then
        new_offenders+=("$o")
    fi
done

if (( ${#new_offenders[@]} > 0 )); then
    echo "radon: ${#new_offenders[@]} function(s) exceed complexity budget (rank D or worse):"
    for o in "${new_offenders[@]}"; do
        echo "  - $o"
    done
    echo ""
    echo "Either refactor below rank D, or add the symbol to .audit/radon-waivers.txt"
    echo "with a one-line justification in reports/findings-matrix.md."
    exit 1
fi

echo "radon: 0 unwaivered functions over budget"
exit 0
