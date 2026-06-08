#!/bin/bash
# backup-state.sh — version-control the SAFE text state of all Hermes profiles.
# Model: ALLOWLIST-COPY (only known-safe files enter the repo) + secret scan + git commit.
# Secrets (.env, auth.json, mcp-tokens/, _token-backups/) are NEVER copied. Three layers:
#   1) allowlist rsync (primary)  2) .gitignore (backstop)  3) pre-commit secret scan (abort).
set -euo pipefail

SRC="$HOME/.hermes"
REPO="$HOME/hermes-state-backup"
PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

mkdir -p "$REPO"

# Per-profile: copy ONLY config.yaml, SOUL.md, AGENTS.md, memories/, skills/
for pdir in "$SRC"/profiles/*/; do
  [ -d "$pdir" ] || continue
  slug="$(basename "$pdir")"
  dest="$REPO/profiles/$slug"
  mkdir -p "$dest"
  rsync -a --delete \
    --include='config.yaml' \
    --include='SOUL.md' \
    --include='AGENTS.md' \
    --include='memories/' --include='memories/***' \
    --include='skills/'   --include='skills/***' \
    --exclude='*' \
    "$pdir" "$dest/"
done

# Top-level (default profile + shared): honcho.json, config.yaml, SOUL.md, memories/, skills/
mkdir -p "$REPO/_global"
rsync -a --delete \
  --include='honcho.json' \
  --include='config.yaml' \
  --include='SOUL.md' \
  --include='memories/' --include='memories/***' \
  --include='skills/'   --include='skills/***' \
  --exclude='*' \
  "$SRC/" "$REPO/_global/"

# ── SECRET SCAN (layer 3): abort the commit if anything secret-shaped slipped in ──
cd "$REPO"
PATTERNS='([0-9]{8,12}:[A-Za-z0-9_-]{30,})'                 # telegram bot token
PATTERNS="$PATTERNS|(sk-(ant-|proj-|or-)?[A-Za-z0-9_-]{16,})" # openai/anthropic/openrouter
PATTERNS="$PATTERNS|(mn_[A-Za-z0-9]{16,})"                   # minimax
PATTERNS="$PATTERNS|(gh[pousr]_[A-Za-z0-9]{20,})"            # github
PATTERNS="$PATTERNS|(eyJ[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{10,})" # JWT
PATTERNS="$PATTERNS|((api[_-]?key|client_secret|access_token|refresh_token|password)[\"'\'' ]*[:=][\"'\'' ]*[A-Za-z0-9._-]{16,})"
if hits=$(grep -rEnI --exclude-dir=.git -e "$PATTERNS" . 2>/dev/null); then
  echo "ABORT: secret-shaped content found in backup — NOT committing:" >&2
  echo "$hits" | head -20 >&2
  exit 1
fi

# Hard assert: none of the forbidden paths exist in the working tree
if find . -path ./.git -prune -o \( -name '.env' -o -name 'auth.json' -o -name 'mcp-tokens' -o -name '_token-backups' \) -print | grep -q .; then
  echo "ABORT: a forbidden secret path exists in the repo tree — NOT committing." >&2
  find . -path ./.git -prune -o \( -name '.env' -o -name 'auth.json' -o -name 'mcp-tokens' \) -print >&2
  exit 1
fi

# ── commit (local only; no remote by design) ──
[ -d .git ] || { git init -q; }
git add -A
if git diff --cached --quiet; then
  echo "no state changes since last snapshot"
else
  git commit -q -m "state snapshot $(date -u +%Y-%m-%dT%H:%MZ)"
  echo "committed: $(git rev-parse --short HEAD)"
fi
