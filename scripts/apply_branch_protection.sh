#!/usr/bin/env bash
# Apply the branch-protection contract from docs/branch-protection.md
# idempotently. Requires `gh` authenticated with admin:repo scope.
#
# Usage:
#   scripts/apply_branch_protection.sh owner/repo
#   scripts/apply_branch_protection.sh owner/repo --dry-run
#
# Re-running this script overwrites the existing protection — that's
# intentional. The contract is the single source of truth for what gates
# must pass before merging to main.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 owner/repo [--dry-run]" >&2
    exit 1
fi

REPO="$1"
DRY_RUN="${2:-}"

if ! command -v gh >/dev/null 2>&1; then
    echo "gh CLI not found. Install: https://cli.github.com/" >&2
    exit 1
fi

# Required status-check contexts. These names MUST match the job names in
# .github/workflows/ci.yml (when that exists). Order doesn't matter to the
# API; keeping it readable here.
CONTEXTS=(
    preflight
    ruff-check
    ruff-format
    mypy-strict
    pytest
    bandit
    radon-budget
    forbidden-patterns
    secret-scan
    pip-audit
    frontend-tsc
    frontend-lint
    frontend-test
    npm-audit
    workflow-contract
    security-regression
)

# Build the --field arguments.
GH_FLAGS=(
    -X PUT
    -H "Accept: application/vnd.github+json"
    "repos/$REPO/branches/main/protection"
    -F required_status_checks.strict=true
    -F required_pull_request_reviews.dismiss_stale_reviews=true
    -F required_pull_request_reviews.required_approving_review_count=1
    -F enforce_admins=true
    -F allow_force_pushes=false
    -F allow_deletions=false
)
for ctx in "${CONTEXTS[@]}"; do
    GH_FLAGS+=(-F "required_status_checks.contexts[]=$ctx")
done

if [[ "$DRY_RUN" == "--dry-run" ]]; then
    echo "Would run: gh api ${GH_FLAGS[*]}"
    exit 0
fi

echo "Applying branch protection to $REPO main..."
gh api "${GH_FLAGS[@]}" >/dev/null

echo ""
echo "Verifying..."
gh api "repos/$REPO/branches/main/protection" --jq '.required_status_checks.contexts'

echo ""
echo "Done. Re-run anytime; the apply is idempotent."
