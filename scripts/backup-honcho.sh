#!/bin/bash
# backup-honcho.sh — logical dump of the Honcho Postgres (peers, user model, observations).
# Fleet-wide: one dump captures all 9 AI peers + the shared user peer. Keeps last 8.
set -euo pipefail
PATH="$HOME/.orbstack/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

OUT="$HOME/backups"
mkdir -p "$OUT"
ts="$(date +%Y%m%d-%H%M%S)"

docker exec -t server-database-1 pg_dump -U honcho honcho | gzip > "$OUT/honcho-$ts.sql.gz"

# verify the dump is non-trivial (gzip header + some content)
sz=$(stat -f%z "$OUT/honcho-$ts.sql.gz" 2>/dev/null || echo 0)
if [ "$sz" -lt 200 ]; then
  echo "WARN: honcho dump suspiciously small ($sz bytes) — check the DB/container" >&2
fi

# rotate: keep newest 8
ls -t "$OUT"/honcho-*.sql.gz 2>/dev/null | tail -n +9 | xargs -r rm -f
echo "honcho dump: $OUT/honcho-$ts.sql.gz ($sz bytes)"
