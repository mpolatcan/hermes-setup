#!/bin/bash
# backup-honcho.sh — logical dump of the Honcho Postgres (peers, user model, observations).
# Fleet-wide: one dump captures all 9 AI peers + the shared user peer. Keeps last 8.
set -euo pipefail
PATH="$HOME/.orbstack/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

OUT="$HOME/backups"
LOG="$OUT/honcho-backup.log"
mkdir -p "$OUT"
ts="$(date +%Y%m%d-%H%M%S)"

fail() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') FAIL: $1" >> "$LOG"
  # notify via hermes so the failure is visible, not silent
  /opt/homebrew/bin/hermes send "⚠️ Honcho backup FAILED: $1" >/dev/null 2>&1 || true
  exit 1
}

# container must be up — a dump against a stopped/paused OrbStack yields an empty file
docker inspect -f '{{.State.Running}}' server-database-1 2>/dev/null | grep -q true \
  || fail "server-database-1 not running (OrbStack down?)"

# dump to a temp file first; only publish after verifying it's real
tmp="$OUT/.honcho-$ts.sql.gz.tmp"
docker exec server-database-1 pg_dump -U honcho honcho | gzip > "$tmp" \
  || { rm -f "$tmp"; fail "pg_dump exited non-zero"; }

sz=$(stat -f%z "$tmp" 2>/dev/null || echo 0)
if [ "$sz" -lt 10000 ]; then
  rm -f "$tmp"
  fail "dump too small ($sz bytes) — DB empty or connection broken"
fi

mv "$tmp" "$OUT/honcho-$ts.sql.gz"

# rotate: keep newest 14 (two runs/day — launchd 03:00 + cron 04:00 — ≈7 days)
ls -t "$OUT"/honcho-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "$(date '+%Y-%m-%d %H:%M:%S') OK: honcho-$ts.sql.gz ($sz bytes)" >> "$LOG"
echo "honcho dump: $OUT/honcho-$ts.sql.gz ($sz bytes)"
