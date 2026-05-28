#!/usr/bin/env bash
# Pre-commit secret scanner (M1-E).
#
# Greps staged-for-commit files (or, with --all, the whole working tree) for
# obvious API-key patterns. Defense against the recurring failure mode of
# this project where keys keep appearing in chat transcripts and .env edits
# (see memory/feedback_no_secrets_in_chat.md). Pre-commit can't stop a key
# from being typed into Claude — but it can stop it from being committed.
#
# Exits non-zero on match so a pre-commit hook fails the commit.
#
# Usage:
#   ./scripts/check_secrets.sh                  # scan only staged files
#   ./scripts/check_secrets.sh --all            # scan the whole working tree
#   ./scripts/check_secrets.sh --files a.py b.py  # scan an explicit list
#
# Wired into git via .pre-commit-config.yaml at the repo root.

set -euo pipefail

# Patterns. Add new ones here as we adopt more providers.
#   sk-ant-api03-...   Anthropic console API key
#   AIzaSy...          Google Cloud / Gemini / Firebase web API key
#   s2k-...            Semantic Scholar API key
#   sk-...{32,}        OpenAI-style secret key
#   xoxb-...           Slack bot token
#   ghp_...{36}        GitHub personal access token
#   ghs_...{36}        GitHub server-side token
PATTERN='(sk-ant-api03-[A-Za-z0-9_-]{20,})|(AIzaSy[A-Za-z0-9_-]{20,})|(s2k-[A-Za-z0-9_-]{20,})|(sk-[A-Za-z0-9]{32,})|(xoxb-[0-9]+-[0-9]+-[A-Za-z0-9]+)|(ghp_[A-Za-z0-9]{36})|(ghs_[A-Za-z0-9]{36})'

mode="staged"
explicit=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --all) mode="all"; shift ;;
        --files) mode="explicit"; shift; while [[ $# -gt 0 && "$1" != --* ]]; do explicit+=("$1"); shift; done ;;
        *) echo "Unknown arg: $1" >&2; exit 2 ;;
    esac
done

case "$mode" in
    staged)
        # Diff against HEAD so we only check what's actually about to be
        # committed. -z + xargs handles paths with spaces.
        mapfile -t files < <(git diff --cached --name-only --diff-filter=ACM)
        ;;
    all)
        mapfile -t files < <(git ls-files)
        ;;
    explicit)
        files=("${explicit[@]}")
        ;;
esac

if [[ ${#files[@]} -eq 0 ]]; then
    echo "scripts/check_secrets.sh: no files to scan ($mode)"
    exit 0
fi

# Allow-list: files that legitimately contain example patterns (docs, this
# scanner itself, gitignored .env templates). Add patterns CAREFULLY — a
# real leak in an allow-listed file would slip through.
ALLOWLIST_RE='^(scripts/check_secrets\.sh|docs/|backend/tests/|backend/\.env\.example|\.pre-commit-config\.yaml)'

violations=0
for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    if [[ "$f" =~ $ALLOWLIST_RE ]]; then
        continue
    fi
    if grep -nE "$PATTERN" "$f" >/dev/null 2>&1; then
        echo ""
        echo "SECRET DETECTED in $f:"
        # Show line numbers but redact the actual match — we don't want to
        # print the key to the terminal log either.
        grep -nE "$PATTERN" "$f" | sed -E 's/(sk-ant-api03-|AIzaSy|s2k-|sk-|xoxb-|ghp_|ghs_)[A-Za-z0-9_-]+/\1***REDACTED***/g'
        violations=$((violations + 1))
    fi
done

if [[ $violations -gt 0 ]]; then
    echo ""
    echo "================================================================"
    echo "$violations file(s) contain what look like API keys."
    echo ""
    echo "If this is a real key:"
    echo "  1. REVOKE it at the provider console immediately."
    echo "  2. Remove it from the file and stage the cleaned version."
    echo "  3. Re-run the commit."
    echo ""
    echo "If this is a false positive, add a tighter allow-list rule in"
    echo "scripts/check_secrets.sh and document why."
    echo "================================================================"
    exit 1
fi

echo "scripts/check_secrets.sh: ${#files[@]} file(s) scanned, no secret patterns detected"
exit 0
