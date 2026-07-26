#!/bin/bash
# Daily native Hermes quick snapshots for all canonical profiles.
set -euo pipefail
umask 077

ROOT="/Users/mutlupolatcan/.hermes"
OUT="$ROOT/backups/hermes"
OPS="$ROOT/scripts/backup_ops.py"
HERMES="/Users/mutlupolatcan/.local/bin/hermes"
SEND="$ROOT/scripts/hermes-send-keychain.sh"
LOG="$OUT/quick-backup.log"
PROFILES="general assistant coder finance health marketing producer researcher writer"

mkdir -p "$OUT"
chmod 700 "$OUT"

fail() {
  local reason="$1"
  trap - ERR
  printf '%s FAIL quick: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$reason" >> "$LOG"
  chmod 600 "$LOG"
  "$SEND" general --to telegram "⚠️ Hermes quick backup FAILED: $reason" >/dev/null 2>&1 || true
  exit 1
}

on_error() {
  local rc="$?"
  local line="$1"
  fail "unexpected error at line $line (exit $rc)"
}
trap 'on_error "$LINENO"' ERR

latest_snapshot_since() {
  python3 - "$1" "$2" <<'PY'
from pathlib import Path
import sys
root = Path(sys.argv[1]) / "state-snapshots"
started = int(sys.argv[2])
items = [p for p in root.iterdir() if p.is_dir() and p.stat().st_mtime >= started - 5]
if not items:
    raise SystemExit(1)
print(max(items, key=lambda p: p.stat().st_mtime))
PY
}

VERIFIED=0
for profile in $PROFILES; do
  home="$ROOT/profiles/$profile"
  started="$(date +%s)"
  if ! output="$(HERMES_HOME="$home" "$HERMES" backup --quick --label scheduled-daily 2>&1)"; then
    fail "native quick backup command failed for $profile"
  fi
  if ! printf '%s\n' "$output" | grep -q 'State snapshot created:'; then
    fail "native quick backup did not confirm snapshot creation for $profile"
  fi
  if printf '%s\n' "$output" | grep -q 'CRITICAL: could not snapshot DB'; then
    fail "SQLite snapshot incomplete for $profile"
  fi
  snapshot="$(latest_snapshot_since "$home" "$started")" || fail "fresh snapshot not created for $profile"
  python3 "$OPS" verify-snapshot "$snapshot" >/dev/null || fail "snapshot verification failed for $profile"
  python3 "$OPS" prune-snapshots "$home/state-snapshots" --keep 7 >/dev/null
  VERIFIED=$((VERIFIED+1))
done

printf '%s OK quick: %s verified profiles\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$VERIFIED" >> "$LOG"
chmod 600 "$LOG"
