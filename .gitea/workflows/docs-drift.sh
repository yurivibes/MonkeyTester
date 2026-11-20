#!/usr/bin/env bash
# ── Documentation drift check ─────────────────────────────────────────────────
# Fails the build if source files changed but AGENTS.md was not updated.
#
# Usage (CI):
#   1. Write changed files list to /tmp/changed_files.txt (one per line)
#   2. Run this script
#
# CI env (Gitea/GitHub Actions):
#   GITHUB_BASE_SHA  – base commit SHA (PR target)
#   GITHUB_HEAD_SHA  – head commit SHA (PR source)
# If neither is set, falls back to HEAD~1..HEAD.

set -euo pipefail

# ── Collect changed files ─────────────────────────────────────────────────────
if [ -f /tmp/changed_files.txt ]; then
  cp /tmp/changed_files.txt /tmp/docs_drift_input.txt
else
  git diff --name-only "${GITHUB_BASE_SHA:-HEAD~1}" "${GITHUB_HEAD_SHA:-HEAD}" > /tmp/docs_drift_input.txt
fi

# ── Classification ────────────────────────────────────────────────────────────
AGENT_CHANGED=0
SOURCE_CHANGED=""

while IFS= read -r file; do
  [ -z "$file" ] && continue

  # AGENTS.md?
  case "$file" in
    */AGENTS.md|AGENTS.md)
      AGENT_CHANGED=1
      ;;
  esac

  # Ignore patterns — files that don't require doc updates
  case "$file" in
    .git/*|README.md|.gitea/*|.github/*|*.lock|*.lockb|*.sum|go.*|vendor/*)
      continue
      ;;
  esac

  # Source pattern check (prefix or exact match)
  case "$file" in
    app.py|startup.sh|index.html|test.html|logs.html|player.html|stream.html|settings.html|configuration.html)
      SOURCE_CHANGED="${SOURCE_CHANGED}
    - $file"
      ;;
    visual-tests/scripts/*)
      SOURCE_CHANGED="${SOURCE_CHANGED}
    - $file"
      ;;
  esac
done < /tmp/docs_drift_input.txt

# ── Report ────────────────────────────────────────────────────────────────────
TOTAL=$(wc -l < /tmp/docs_drift_input.txt)
echo "Changed files: $TOTAL"
echo "AGENTS.md updated: $([ $AGENT_CHANGED -eq 1 ] && echo 'yes' || echo 'no')"

if [ -n "$SOURCE_CHANGED" ]; then
  echo ""
  echo "Source files changed:$SOURCE_CHANGED"
fi

# ── Decision ──────────────────────────────────────────────────────────────────
if [ -n "$SOURCE_CHANGED" ] && [ $AGENT_CHANGED -eq 0 ]; then
  echo ""
  echo "============================================================"
  echo "❌ FAIL: Documentation drift detected"
  echo "============================================================"
  echo ""
  echo "Source files changed but AGENTS.md was NOT updated."
  echo "Updated source files:$SOURCE_CHANGED"
  echo ""
  echo "Fix: Update AGENTS.md to reflect these changes, then commit."
  echo "============================================================"
  exit 1
fi

echo ""
echo "✅ OK: No documentation drift"
exit 0
