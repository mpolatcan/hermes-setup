#!/bin/bash
# Weekly isolated restore drill for the newest Honcho logical backup.
set -euo pipefail
umask 077

PATH="/Users/mutlupolatcan/.orbstack/bin:/opt/homebrew/bin:/usr/bin:/bin"
export DOCKER_HOST="${DOCKER_HOST:-unix:///Users/mutlupolatcan/.orbstack/run/docker.sock}"
OUT="/Users/mutlupolatcan/.hermes/backups/honcho"
OPS="/Users/mutlupolatcan/.hermes/scripts/backup_ops.py"
SEND="/Users/mutlupolatcan/.hermes/scripts/hermes-send-keychain.sh"
CONTAINER="honcho-restore-drill-$(date +%Y%m%d%H%M%S)"
PASSWORD="restore-$(uuidgen | tr '[:upper:]' '[:lower:]')"
REPORT="$OUT/restore-drill-latest.json"
REPORT_PARTIAL="$OUT/.restore-drill-latest.json.partial"
START="$(date +%s)"

mkdir -p "$OUT"
chmod 700 "$OUT"

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -f "$REPORT_PARTIAL"
}
trap cleanup EXIT

fail() {
  local reason="$1"
  "$SEND" general --to telegram "⚠️ Honcho restore drill FAILED: $reason" >/dev/null 2>&1 || true
  echo "$reason" >&2
  exit 1
}

LATEST="$(python3 - "$OUT" <<'PY'
from pathlib import Path
import sys
items = sorted(Path(sys.argv[1]).glob('honcho-*.sql.gz'), key=lambda p: p.stat().st_mtime, reverse=True)
if not items:
    raise SystemExit(1)
print(items[0])
PY
)" || fail "no Honcho backup found"

python3 "$OPS" verify-gzip "$LATEST" >/dev/null || fail "gzip verification failed"
[ -f "$LATEST.sha256" ] || fail "checksum manifest missing"
(cd "$OUT" && shasum -a 256 -c "$(basename "$LATEST").sha256" >/dev/null) || fail "checksum mismatch"

docker run --rm -d --name "$CONTAINER" \
  -e POSTGRES_PASSWORD="$PASSWORD" \
  -e POSTGRES_DB=honcho \
  pgvector/pgvector:pg15 >/dev/null

READY=false
for _ in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U postgres -d honcho >/dev/null 2>&1; then READY=true; break; fi
  sleep 1
done
[ "$READY" = true ] || fail "temporary PostgreSQL did not become ready"

docker exec "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d honcho \
  -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='honcho') THEN CREATE ROLE honcho; END IF; END \$\$;" >/dev/null

gzip -dc "$LATEST" | docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d honcho >/dev/null \
  || fail "logical restore failed"

COUNTS="$(docker exec "$CONTAINER" psql -v ON_ERROR_STOP=1 -U postgres -d honcho -At -F $'\t' -c "
SELECT
  (SELECT count(*) FROM information_schema.tables WHERE table_schema='public'),
  COALESCE((SELECT count(*) FROM workspaces),0),
  COALESCE((SELECT count(*) FROM sessions),0),
  COALESCE((SELECT count(*) FROM messages),0),
  COALESCE((SELECT string_agg(version_num, ',') FROM alembic_version),'');
")" || fail "restored schema validation query failed"

IFS=$'\t' read -r TABLES WORKSPACES SESSIONS MESSAGES MIGRATION <<< "$COUNTS"
[ "${TABLES:-0}" -ge 10 ] || fail "restored schema has too few tables: ${TABLES:-0}"
[ -n "${MIGRATION:-}" ] || fail "restored alembic version missing"
END="$(date +%s)"
DURATION=$((END-START))

python3 - "$REPORT_PARTIAL" "$LATEST" "$TABLES" "$WORKSPACES" "$SESSIONS" "$MESSAGES" "$MIGRATION" "$DURATION" <<'PY'
import json,sys,datetime
path,backup,tables,workspaces,sessions,messages,migration,duration=sys.argv[1:]
payload={
  'status':'ok',
  'verified_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
  'backup':backup,
  'tables':int(tables),
  'workspaces':int(workspaces),
  'sessions':int(sessions),
  'messages':int(messages),
  'migration':migration,
  'duration_seconds':int(duration),
}
with open(path,'w',encoding='utf-8') as fh: json.dump(payload,fh,indent=2)
PY
chmod 600 "$REPORT_PARTIAL"
mv "$REPORT_PARTIAL" "$REPORT"
chmod 600 "$REPORT"
printf 'restore drill ok: %s tables, %s sessions, %ss\n' "$TABLES" "$SESSIONS" "$DURATION"
