#!/bin/bash
# Canonical Honcho PostgreSQL backup. Called only by launchd or thin wrappers.
set -euo pipefail
umask 077

PATH="/Users/mutlupolatcan/.orbstack/bin:/opt/homebrew/bin:/usr/bin:/bin"
export DOCKER_HOST="${DOCKER_HOST:-unix:///Users/mutlupolatcan/.orbstack/run/docker.sock}"
OUT="/Users/mutlupolatcan/.hermes/backups/honcho"
OPS="/Users/mutlupolatcan/.hermes/scripts/backup_ops.py"
SEND="/Users/mutlupolatcan/.hermes/scripts/hermes-send-keychain.sh"
LOG="$OUT/honcho-backup.log"
TS="$(date +%Y%m%d-%H%M%S)"
FINAL="$OUT/honcho-$TS.sql.gz"
PARTIAL="$OUT/.honcho-$TS.sql.gz.partial"

mkdir -p "$OUT"
chmod 700 "$OUT"
find "$OUT" -maxdepth 1 -type f -exec chmod 600 {} + 2>/dev/null || true

fail() {
  local reason="$1"
  rm -f "$PARTIAL"
  printf '%s FAIL: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$reason" >> "$LOG"
  chmod 600 "$LOG"
  "$SEND" general --to telegram "⚠️ Honcho backup FAILED: $reason" >/dev/null 2>&1 || true
  exit 1
}
trap 'fail "unexpected error at line $LINENO"' ERR

[ "$(docker inspect -f '{{.State.Running}}' server-database-1 2>/dev/null)" = "true" ] \
  || fail "server-database-1 not running"

docker exec server-database-1 pg_dump --no-password -U honcho honcho | gzip -9 > "$PARTIAL"
chmod 600 "$PARTIAL"
SIZE="$(stat -f%z "$PARTIAL")"
[ "$SIZE" -ge 10000 ] || fail "dump too small ($SIZE bytes)"
python3 "$OPS" verify-gzip "$PARTIAL" >/dev/null

gzip -t "$PARTIAL"
mv "$PARTIAL" "$FINAL"
chmod 600 "$FINAL"
python3 "$OPS" write-sha256 "$FINAL" >/dev/null
python3 "$OPS" prune "$OUT/honcho-*.sql.gz" --keep 14 >/dev/null

printf '%s OK: %s (%s bytes)\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$(basename "$FINAL")" "$SIZE" >> "$LOG"
chmod 600 "$LOG"
printf 'honcho dump: %s (%s bytes)\n' "$FINAL" "$SIZE"
