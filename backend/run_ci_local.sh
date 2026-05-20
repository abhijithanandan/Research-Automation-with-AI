#!/usr/bin/env bash
# Run CI checks locally before pushing. Must all pass before opening a PR.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> ruff check"
ruff check .

echo "==> ruff format --check"
ruff format --check .

echo "==> mypy"
mypy app

echo ""
echo "All checks passed."
