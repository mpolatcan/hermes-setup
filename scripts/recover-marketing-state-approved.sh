#!/bin/bash
set -Eeuo pipefail

umask 077

PROFILE="marketing"
EXPECTED_UID="501"
EXPECTED_SESSIONS="26"
EXPECTED_MESSAGES="321"
EXPECTED_ORPHANS="0"
PROFILE_HOME="/Users/mutlupolatcan/.hermes/profiles/$PROFILE"
LIVE_DB="$PROFILE_HOME/state.db"
PLIST="/Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.gateway-marketing.plist"
LABEL="ai.hermes.gateway-marketing"
SERVICE="gui/$EXPECTED_UID/$LABEL"
PORT="8793"
RECOVERY_ROOT="$PROFILE_HOME/state-recovery"
STAMP="$(date +%Y%m%d-%H%M%S)"
RECOVERY_DIR="$RECOVERY_ROOT/$STAMP"
RAW_DIR="$RECOVERY_DIR/raw"
WORK_DIR="$RECOVERY_DIR/working"
RECOVER_SQL="$RECOVERY_DIR/recover.sql"
RECOVERED_DB="$RECOVERY_DIR/state.recovered.db"
CORRUPT_LIVE="$RECOVERY_DIR/state.db.corrupt-live"
CORRUPT_WAL="$RECOVERY_DIR/state.db-wal.corrupt-live"
CORRUPT_SHM="$RECOVERY_DIR/state.db-shm.corrupt-live"
FAILED_RECOVERED="$RECOVERY_DIR/state.db.failed-recovered"
FAILED_WAL="$RECOVERY_DIR/state.db-wal.failed-recovered"
FAILED_SHM="$RECOVERY_DIR/state.db-shm.failed-recovered"
SERVICE_STOPPED=0
QUIESCENT=0
BOOTSTRAP_ATTEMPTED=0
SWAP_STARTED=0
SWAPPED=0

log() {
  printf '[marketing-recovery] %s\n' "$*"
}

require_regular_owned_file() {
  path="$1"
  [ -f "$path" ] || { log "required file missing: $path"; return 1; }
  [ ! -L "$path" ] || { log "symlink rejected: $path"; return 1; }
  [ "$(stat -f '%u' "$path")" = "$EXPECTED_UID" ] || {
    log "wrong owner for $path"
    return 1
  }
}

require_real_owned_dir() {
  path="$1"
  [ -d "$path" ] || { log "required directory missing: $path"; return 1; }
  [ ! -L "$path" ] || { log "directory symlink rejected: $path"; return 1; }
  [ "$(stat -f '%u' "$path")" = "$EXPECTED_UID" ] || {
    log "wrong directory owner: $path"
    return 1
  }
}

no_live_handles() {
  for candidate in "$LIVE_DB" "$LIVE_DB-wal" "$LIVE_DB-shm"; do
    [ -e "$candidate" ] || continue
    output=""
    if output="$(lsof "$candidate" 2>&1)"; then
      log "live SQLite handle probe returned success; not accepting absence: $candidate"
      return 1
    else
      rc=$?
      if [ "$rc" -ne 1 ] || [ -n "$output" ]; then
        log "could not prove handle state for $candidate: rc=$rc"
        return 1
      fi
    fi
  done
}

wait_for_quiescence() {
  for _ in $(seq 1 30); do
    launch_output=""
    if launch_output="$(launchctl print "$SERVICE" 2>&1)"; then
      sleep 1
      continue
    else
      launch_rc=$?
      expected_launch_output="$(printf 'Bad request.\nCould not find service "%s" in domain for user gui: %s' "$LABEL" "$EXPECTED_UID")"
      if [ "$launch_rc" -ne 113 ] || [ "$launch_output" != "$expected_launch_output" ]; then
        log "ambiguous launchctl probe failure: rc=$launch_rc"
        return 1
      fi
    fi

    listener_output=""
    if listener_output="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>&1)"; then
      log "listener probe returned success; service may still own port $PORT"
      return 1
    else
      listener_rc=$?
      if [ "$listener_rc" -ne 1 ] || [ -n "$listener_output" ]; then
        log "ambiguous listener probe failure: rc=$listener_rc"
        return 1
      fi
    fi

    if no_live_handles; then
      QUIESCENT=1
      return 0
    fi
    sleep 1
  done
  return 1
}

reprove_quiescence() {
  QUIESCENT=0
  wait_for_quiescence
}

stop_service_strict() {
  if launchctl print "$SERVICE" >/dev/null 2>&1; then
    launchctl bootout "$SERVICE"
  fi
  SERVICE_STOPPED=1
  QUIESCENT=0
  wait_for_quiescence
}

start_service() {
  [ "$QUIESCENT" -eq 1 ] || {
    log "refusing bootstrap without quiescence proof"
    return 1
  }
  BOOTSTRAP_ATTEMPTED=1
  QUIESCENT=0
  launchctl bootstrap "gui/$EXPECTED_UID" "$PLIST"
  SERVICE_STOPPED=0
}

rollback() {
  rc="${1:-1}"
  trap - ERR INT TERM
  log "failure rc=$rc; starting rollback"

  if [ "$SWAP_STARTED" -eq 1 ]; then
    # Never trust a previous quiescence observation during rollback.  A failed
    # bootstrap or another actor may have opened the replacement meanwhile.
    QUIESCENT=0
    if ! stop_service_strict; then
      log "CRITICAL: rollback deferred; could not prove DB quiescence, no database files were touched"
      exit "$rc"
    fi
  fi

  if [ "$SWAP_STARTED" -eq 1 ] && [ "$QUIESCENT" -eq 1 ]; then
    replacement_present=0
    if [ -f "$CORRUPT_LIVE" ] && [ -f "$LIVE_DB" ]; then
      replacement_present=1
    fi

    if [ -f "$CORRUPT_LIVE" ]; then
      if [ -f "$LIVE_DB" ]; then
        mv "$LIVE_DB" "$FAILED_RECOVERED"
      fi
      mv "$CORRUPT_LIVE" "$LIVE_DB"
      chmod 600 "$LIVE_DB"
    fi

    if [ -f "$CORRUPT_WAL" ]; then
      if [ -f "$LIVE_DB-wal" ]; then
        mv "$LIVE_DB-wal" "$FAILED_WAL"
      fi
      mv "$CORRUPT_WAL" "$LIVE_DB-wal"
      chmod 600 "$LIVE_DB-wal"
    elif [ "$replacement_present" -eq 1 ] && [ -f "$LIVE_DB-wal" ]; then
      mv "$LIVE_DB-wal" "$FAILED_WAL"
    fi

    if [ -f "$CORRUPT_SHM" ]; then
      if [ -f "$LIVE_DB-shm" ]; then
        mv "$LIVE_DB-shm" "$FAILED_SHM"
      fi
      mv "$CORRUPT_SHM" "$LIVE_DB-shm"
      chmod 600 "$LIVE_DB-shm"
    elif [ "$replacement_present" -eq 1 ] && [ -f "$LIVE_DB-shm" ]; then
      mv "$LIVE_DB-shm" "$FAILED_SHM"
    fi

    SWAPPED=0
    SWAP_STARTED=0
  fi

  if [ "$SERVICE_STOPPED" -eq 1 ] && [ "$QUIESCENT" -eq 1 ]; then
    start_service || true
  fi
  exit "$rc"
}
main() {
trap 'rollback $?' ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM

[ "$(id -u)" = "$EXPECTED_UID" ]
[ "$(id -un)" = "mutlupolatcan" ]
[ "$EXPECTED_UID" != "0" ]
require_real_owned_dir "$PROFILE_HOME"
require_regular_owned_file "$LIVE_DB"
require_regular_owned_file "$PLIST"
[ "$(cd "$PROFILE_HOME" && pwd -P)" = "$PROFILE_HOME" ]
[ "$(plutil -extract Label raw -o - "$PLIST")" = "$LABEL" ]
[ "$(plutil -extract ProgramArguments.2 raw -o - "$PLIST")" = "$PROFILE" ]
[ "$(plutil -extract WorkingDirectory raw -o - "$PLIST")" = "$PROFILE_HOME" ]
[ "$(plutil -extract EnvironmentVariables.HERMES_HOME raw -o - "$PLIST")" = "$PROFILE_HOME" ]
launchctl print "$SERVICE" >/dev/null
command -v sqlite3 >/dev/null
command -v launchctl >/dev/null
command -v lsof >/dev/null
command -v shasum >/dev/null
command -v plutil >/dev/null

if [ -e "$RECOVERY_ROOT" ]; then
  require_real_owned_dir "$RECOVERY_ROOT"
  [ "$(stat -f '%Lp' "$RECOVERY_ROOT")" = "700" ]
else
  mkdir -m 700 "$RECOVERY_ROOT"
fi
mkdir -m 700 "$RECOVERY_DIR"
mkdir -m 700 "$RAW_DIR"
mkdir -m 700 "$WORK_DIR"

log "stopping $SERVICE"
stop_service_strict

log "capturing stopped raw state with byte parity"
: > "$RECOVERY_DIR/raw.sha256"
chmod 600 "$RECOVERY_DIR/raw.sha256"
for suffix in '' '-wal' '-shm'; do
  source_file="$LIVE_DB$suffix"
  if [ -e "$source_file" ]; then
    require_regular_owned_file "$source_file"
    raw_file="$RAW_DIR/state.db$suffix"
    [ ! -e "$raw_file" ]
    cp -p "$source_file" "$raw_file"
    chmod 600 "$raw_file"
    source_hash="$(shasum -a 256 "$source_file" | cut -d' ' -f1)"
    copy_hash="$(shasum -a 256 "$raw_file" | cut -d' ' -f1)"
    [ "$source_hash" = "$copy_hash" ]
    printf '%s  source%s\n%s  raw/state.db%s\n' \
      "$source_hash" "$suffix" "$copy_hash" "$suffix" >> "$RECOVERY_DIR/raw.sha256"
  else
    printf 'absent  source%s\n' "$suffix" >> "$RECOVERY_DIR/raw.sha256"
  fi
done
no_live_handles

for raw_file in "$RAW_DIR"/state.db*; do
  [ -f "$raw_file" ]
  cp -p "$raw_file" "$WORK_DIR/$(basename "$raw_file")"
done
chmod 600 "$WORK_DIR"/*
WORK_DB="$WORK_DIR/state.db"

BASE_COUNTS="$(sqlite3 -batch -noheader -separator '|' "$WORK_DB" \
  "SELECT (SELECT count(*) FROM sessions),
          (SELECT count(*) FROM messages NOT INDEXED),
          (SELECT count(*) FROM messages AS m NOT INDEXED LEFT JOIN sessions AS s ON s.id=m.session_id WHERE s.id IS NULL);")"
IFS='|' read -r BASE_SESSIONS BASE_MESSAGES BASE_ORPHANS <<EOF
$BASE_COUNTS
EOF
[ "$BASE_SESSIONS" = "$EXPECTED_SESSIONS" ]
[ "$BASE_MESSAGES" = "$EXPECTED_MESSAGES" ]
[ "$BASE_ORPHANS" = "$EXPECTED_ORPHANS" ]
log "approved identity sessions=$BASE_SESSIONS messages=$BASE_MESSAGES orphans=$BASE_ORPHANS"

log "recovering from working copy into isolated database"
sqlite3 "$WORK_DB" '.recover --ignore-freelist' > "$RECOVER_SQL"
chmod 600 "$RECOVER_SQL"
sqlite3 -bail "$RECOVERED_DB" < "$RECOVER_SQL"
sqlite3 -bail "$RECOVERED_DB" \
  "INSERT INTO messages_fts(messages_fts) VALUES('rebuild');
   INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('rebuild');
   REINDEX;
   INSERT INTO messages_fts(messages_fts, rank) VALUES('integrity-check', 1);
   INSERT INTO messages_fts_trigram(messages_fts_trigram, rank) VALUES('integrity-check', 1);"
chmod 600 "$RECOVERED_DB"

QUICK_CHECK="$(sqlite3 -batch -noheader "$RECOVERED_DB" 'PRAGMA quick_check;')"
[ "$QUICK_CHECK" = "ok" ]
RECOVERED_COUNTS="$(sqlite3 -batch -noheader -separator '|' "$RECOVERED_DB" \
  "SELECT (SELECT count(*) FROM sessions),
          (SELECT count(*) FROM messages NOT INDEXED),
          (SELECT count(*) FROM messages AS m NOT INDEXED LEFT JOIN sessions AS s ON s.id=m.session_id WHERE s.id IS NULL);")"
[ "$RECOVERED_COUNTS" = "$BASE_COUNTS" ]
FTS_COUNTS="$(sqlite3 -batch -noheader -separator '|' "$RECOVERED_DB" \
  "SELECT
     (SELECT count(*) FROM messages NOT INDEXED),
     (SELECT count(*) FROM messages_fts),
     (SELECT count(*) FROM messages_fts_trigram),
     (SELECT count(*) FROM messages AS m NOT INDEXED LEFT JOIN messages_fts AS f ON f.rowid=m.id
       WHERE f.rowid IS NULL OR f.content != COALESCE(m.content,'') || ' ' || COALESCE(m.tool_name,'') || ' ' || COALESCE(m.tool_calls,'')),
     (SELECT count(*) FROM messages AS m NOT INDEXED LEFT JOIN messages_fts_trigram AS f ON f.rowid=m.id
       WHERE f.rowid IS NULL OR f.content != COALESCE(m.content,'') || ' ' || COALESCE(m.tool_name,'') || ' ' || COALESCE(m.tool_calls,''));")"
[ "$FTS_COUNTS" = "$EXPECTED_MESSAGES|$EXPECTED_MESSAGES|$EXPECTED_MESSAGES|0|0" ]
log "isolated acceptance quick_check=ok counts=$RECOVERED_COUNTS fts=$FTS_COUNTS"

reprove_quiescence
log "performing atomic swap"
SWAP_STARTED=1
if [ -f "$LIVE_DB-wal" ]; then
  mv "$LIVE_DB-wal" "$CORRUPT_WAL"
fi
if [ -f "$LIVE_DB-shm" ]; then
  mv "$LIVE_DB-shm" "$CORRUPT_SHM"
fi
mv "$LIVE_DB" "$CORRUPT_LIVE"
mv "$RECOVERED_DB" "$LIVE_DB"
chmod 600 "$LIVE_DB"
SWAPPED=1

POST_CHECK="$(sqlite3 -batch -noheader "$LIVE_DB" 'PRAGMA quick_check;')"
[ "$POST_CHECK" = "ok" ]
POST_COUNTS="$(sqlite3 -batch -noheader -separator '|' "$LIVE_DB" \
  "SELECT (SELECT count(*) FROM sessions),(SELECT count(*) FROM messages NOT INDEXED),(SELECT count(*) FROM messages AS m NOT INDEXED LEFT JOIN sessions AS s ON s.id=m.session_id WHERE s.id IS NULL);")"
[ "$POST_COUNTS" = "$RECOVERED_COUNTS" ]
POST_FTS="$(sqlite3 -batch -noheader -separator '|' "$LIVE_DB" \
  "SELECT (SELECT count(*) FROM messages_fts),(SELECT count(*) FROM messages_fts_trigram);")"
[ "$POST_FTS" = "$EXPECTED_MESSAGES|$EXPECTED_MESSAGES" ]

log "starting marketing gateway"
start_service
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health" >/dev/null

trap - ERR INT TERM
log "SUCCESS recovery_dir=$RECOVERY_DIR quick_check=ok sessions=$BASE_SESSIONS messages=$BASE_MESSAGES orphans=0 fts=$POST_FTS"
}

if [ "${HERMES_RECOVERY_SOURCE_ONLY:-0}" != "1" ]; then
  main "$@"
fi
