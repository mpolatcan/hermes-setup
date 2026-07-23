#!/bin/bash
# config-snapshot.sh — daily auto-commit of the fleet config surface.
# Safety net for changes made by agents (Derya has config rights) or hermes
# migrations. Meaningful edits should still get manual commits with messages.
set -euo pipefail
PATH="/opt/homebrew/bin:/usr/bin:/bin"
cd /Users/mutlupolatcan/.hermes

git add -A

# nothing staged → nothing to do
git diff --cached --quiet && exit 0

# refuse to commit if anything secret-shaped slipped into tracked files
if git diff --cached | grep -qE "ctx7sk-[a-zA-Z0-9-]{10,}|sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{20,}|xoxb-[a-zA-Z0-9-]{20,}|[0-9]{9,10}:AA[a-zA-Z0-9_-]{30,}"; then
  git reset -q
  /Users/mutlupolatcan/.hermes/runtime/hermes-agent/venv/bin/hermes send "⚠️ config-snapshot: secret-shaped content staged — commit refused, fix the config" >/dev/null 2>&1 || true
  exit 1
fi

git commit -q -m "auto: config snapshot $(date '+%Y-%m-%d %H:%M')"
