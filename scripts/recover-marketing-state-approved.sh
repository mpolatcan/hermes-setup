#!/bin/bash
set -Eeuo pipefail

umask 077

PROFILE="marketing"
EXPECTED_UID="501"
EXPECTED_SESSIONS="26"
EXPECTED_MESSAGES="324"
EXPECTED_ORPHANS="0"
EXPECTED_DUPLICATE_MESSAGE_ID="387"
EXPECTED_DUPLICATE_SESSION_ID="20260712_223038_7a939f39"
EXPECTED_DUPLICATE_OLDER_ROLE="user"
EXPECTED_DUPLICATE_OLDER_TIMESTAMP="1785990555"
EXPECTED_DUPLICATE_OLDER_CONTENT_LENGTH="662"
EXPECTED_DUPLICATE_LATER_ROLE="assistant"
EXPECTED_DUPLICATE_LATER_TIMESTAMP="1785996761.8354"
EXPECTED_DUPLICATE_LATER_CONTENT_LENGTH="39"
EXPECTED_DUPLICATE_OLDER_ROW_SHA3="3bd4006bdf956d74dc935493f48726af5eab99e0b75408bc186d329a1eed1b61"
EXPECTED_DUPLICATE_LATER_ROW_SHA3="3168e115e5c7032592d167e3b13096cf62ea05f74b509ed931021df4ed7f48b2"
EXPECTED_MESSAGES_SCHEMA_SHA3="ca1f913197ba1eb5994812f01f349cc9dc3a2b8b8170122c2b60776b979d0d5d"
EXPECTED_CANONICAL_TABLES="async_delegations;compression_locks;delivery_obligations;gateway_routing;schema_version;session_model_usage;sessions;state_meta"
EXPECTED_RECOVERED_MAX_MESSAGE_ID="420"
EXPECTED_REKEYED_MESSAGE_ID="421"
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

run_probe() {
  # Expected negative probe results must not invoke the inherited main ERR
  # trap inside command-substitution subshells.
  trap - ERR
  "$@"
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
    if output="$(run_probe lsof "$candidate" 2>&1)"; then
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
    if launch_output="$(run_probe launchctl print "$SERVICE" 2>&1)"; then
      sleep 1
      continue
    else
      launch_rc=$?
      expected_launch_output="$(printf 'Bad request.\nCould not find service "%s" in domain for user gui: %s' "$LABEL" "$EXPECTED_UID")"
      if [ "$launch_rc" -ne 113 ]; then
        log "ambiguous launchctl probe failure: rc=$launch_rc"
        return 1
      fi
      if [ "$launch_output" != "$expected_launch_output" ]; then
        log "transient non-canonical launchctl absence; retrying"
        sleep 1
        continue
      fi
    fi

    listener_output=""
    if listener_output="$(run_probe lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>&1)"; then
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

verify_service_preflight() {
  preflight_launch_output=""
  if preflight_launch_output="$(run_probe launchctl print "$SERVICE" 2>&1)"; then
    return 0
  else
    preflight_launch_rc=$?
  fi
  expected_launch_output="$(printf 'Bad request.\nCould not find service "%s" in domain for user gui: %s' "$LABEL" "$EXPECTED_UID")"
  [ "$preflight_launch_rc" -eq 113 ] \
    && [ "$preflight_launch_output" = "$expected_launch_output" ]
}

verify_non_message_table_parity() {
  local source_tables recovered_tables table source_sha3 recovered_sha3

  source_tables="$(sqlite3 -batch -noheader "$WORK_DB" \
    "SELECT group_concat(name,';')
       FROM (
         SELECT name
           FROM sqlite_master
          WHERE type='table'
            AND name NOT LIKE 'messages_fts%'
            AND name NOT IN ('messages','sqlite_sequence')
          ORDER BY name
       );")"
  recovered_tables="$(sqlite3 -batch -noheader "$RECOVERED_DB" \
    "SELECT group_concat(name,';')
       FROM (
         SELECT name
           FROM sqlite_master
          WHERE type='table'
            AND name NOT LIKE 'messages_fts%'
            AND name NOT IN ('messages','sqlite_sequence')
          ORDER BY name
       );")"
  [ "$source_tables" = "$EXPECTED_CANONICAL_TABLES" ]
  [ "$recovered_tables" = "$EXPECTED_CANONICAL_TABLES" ]

  while IFS= read -r table; do
    [ -n "$table" ]
    source_sha3="$(printf '.sha3sum --schema --sha3-256 %s\n' "$table" | sqlite3 -batch "$WORK_DB")"
    recovered_sha3="$(printf '.sha3sum --schema --sha3-256 %s\n' "$table" | sqlite3 -batch "$RECOVERED_DB")"
    [ -n "$source_sha3" ]
    [ "$source_sha3" = "$recovered_sha3" ]
  done < <(printf '%s\n' "$EXPECTED_CANONICAL_TABLES" | tr ';' '\n')
}

salvage_duplicate_message_collision() {
  local duplicate_summary reference_count physical_count recovered_count expected_recovered_count
  local collision_fingerprint expected_fingerprint collision_payload_fingerprint expected_payload_fingerprint
  local timestamp_summary collision_rows collision_distinct_timestamps collision_min_timestamp collision_max_timestamp
  local recovered_max calculated_new_id new_id post_summary sequence_before sequence_after work_db_sql
  local source_column_names recovered_column_names recovered_schema_sha3
  local source_noncollision_sha3 recovered_noncollision_sha3 expected_full_sha3 recovered_full_sha3

  duplicate_summary="$(sqlite3 -batch -noheader -separator '|' "$WORK_DB" \
    "SELECT count(*),min(physical_id),max(physical_id),min(copies),max(copies)
       FROM (
         SELECT id+0 AS physical_id,count(*) AS copies
           FROM messages NOT INDEXED
          GROUP BY id+0
         HAVING count(*)>1
       );")"
  [ "$duplicate_summary" = "1|$EXPECTED_DUPLICATE_MESSAGE_ID|$EXPECTED_DUPLICATE_MESSAGE_ID|2|2" ]

  reference_count="$(sqlite3 -batch -noheader "$WORK_DB" \
    "SELECT count(*)
       FROM sqlite_schema AS s, pragma_foreign_key_list(s.name) AS fk
      WHERE s.type='table' AND lower(fk.\"table\")='messages';")"
  [ "$reference_count" = "0" ]

  collision_fingerprint="$(sqlite3 -batch -noheader "$WORK_DB" \
    "SELECT group_concat(piece,';')
       FROM (
         SELECT printf('%s|%s|%.17g|%d|%s|%s|%s',
                       session_id,role,timestamp,length(content),
                       quote(observed),quote(active),quote(compacted)) AS piece
           FROM messages NOT INDEXED
          WHERE id+0=$EXPECTED_DUPLICATE_MESSAGE_ID
          ORDER BY timestamp
       );")"
  expected_fingerprint="$EXPECTED_DUPLICATE_SESSION_ID|$EXPECTED_DUPLICATE_OLDER_ROLE|$EXPECTED_DUPLICATE_OLDER_TIMESTAMP|$EXPECTED_DUPLICATE_OLDER_CONTENT_LENGTH|0|1|0;$EXPECTED_DUPLICATE_SESSION_ID|$EXPECTED_DUPLICATE_LATER_ROLE|$EXPECTED_DUPLICATE_LATER_TIMESTAMP|$EXPECTED_DUPLICATE_LATER_CONTENT_LENGTH|0|1|0"
  [ "$collision_fingerprint" = "$expected_fingerprint" ]

  collision_payload_fingerprint="$(sqlite3 -batch -noheader "$WORK_DB" \
    "SELECT group_concat(row_sha3,';')
       FROM (
         SELECT lower(hex(sha3(json_array(
                  session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,
                  token_count,finish_reason,reasoning,reasoning_content,reasoning_details,
                  codex_reasoning_items,codex_message_items,platform_message_id,observed,
                  active,compacted,effect_disposition,api_content,display_kind,display_metadata
                ),256))) AS row_sha3
           FROM messages NOT INDEXED
          WHERE id+0=$EXPECTED_DUPLICATE_MESSAGE_ID
          ORDER BY timestamp
       );")"
  expected_payload_fingerprint="$EXPECTED_DUPLICATE_OLDER_ROW_SHA3;$EXPECTED_DUPLICATE_LATER_ROW_SHA3"
  [ "$collision_payload_fingerprint" = "$expected_payload_fingerprint" ]

  source_column_names="$(sqlite3 -batch -noheader "$WORK_DB" \
    "SELECT group_concat(cid || ':' || name || ':' || hidden,';') FROM pragma_table_xinfo('messages');")"
  recovered_column_names="$(sqlite3 -batch -noheader "$RECOVERED_DB" \
    "SELECT group_concat(cid || ':' || name || ':' || hidden,';') FROM pragma_table_xinfo('messages');")"
  [ "$source_column_names" = "$recovered_column_names" ]
  recovered_schema_sha3="$(sqlite3 -batch -noheader "$RECOVERED_DB" \
    "SELECT lower(hex(sha3(group_concat(piece,';'),256)))
       FROM (
         SELECT cid || ':' || name || ':' || type || ':' || \"notnull\" || ':' ||
                quote(dflt_value) || ':' || pk || ':' || hidden AS piece
           FROM pragma_table_xinfo('messages')
          ORDER BY cid
       );")"
  [ "$recovered_schema_sha3" = "$EXPECTED_MESSAGES_SCHEMA_SHA3" ]

  source_noncollision_sha3="$(sqlite3 -batch -noheader "$WORK_DB" \
    "SELECT lower(hex(sha3(group_concat(row_sha3,';'),256)))
       FROM (
         SELECT lower(hex(sha3(json_array(
                  id+0,session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,
                  token_count,finish_reason,reasoning,reasoning_content,reasoning_details,
                  codex_reasoning_items,codex_message_items,platform_message_id,observed,
                  active,compacted,effect_disposition,api_content,display_kind,display_metadata
                ),256))) AS row_sha3
           FROM messages NOT INDEXED
          WHERE id+0!=$EXPECTED_DUPLICATE_MESSAGE_ID
          ORDER BY id+0
       );")"
  recovered_noncollision_sha3="$(sqlite3 -batch -noheader "$RECOVERED_DB" \
    "SELECT lower(hex(sha3(group_concat(row_sha3,';'),256)))
       FROM (
         SELECT lower(hex(sha3(json_array(
                  id,session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,
                  token_count,finish_reason,reasoning,reasoning_content,reasoning_details,
                  codex_reasoning_items,codex_message_items,platform_message_id,observed,
                  active,compacted,effect_disposition,api_content,display_kind,display_metadata
                ),256))) AS row_sha3
           FROM messages NOT INDEXED
          WHERE id!=$EXPECTED_DUPLICATE_MESSAGE_ID
          ORDER BY id
       );")"
  [ "$source_noncollision_sha3" = "$recovered_noncollision_sha3" ]

  timestamp_summary="$(sqlite3 -batch -noheader -separator '|' "$WORK_DB" \
    "SELECT count(*),count(DISTINCT timestamp),min(timestamp),max(timestamp)
       FROM messages NOT INDEXED
      WHERE id+0=$EXPECTED_DUPLICATE_MESSAGE_ID;")"
  IFS='|' read -r collision_rows collision_distinct_timestamps collision_min_timestamp collision_max_timestamp <<EOF
$timestamp_summary
EOF
  [ "$collision_rows" = "2" ]
  [ "$collision_distinct_timestamps" = "2" ]

  physical_count="$(sqlite3 -batch -noheader "$WORK_DB" 'SELECT count(*) FROM messages NOT INDEXED;')"
  recovered_count="$(sqlite3 -batch -noheader "$RECOVERED_DB" 'SELECT count(*) FROM messages NOT INDEXED;')"
  [[ "$physical_count" =~ ^[0-9]+$ ]]
  [[ "$recovered_count" =~ ^[0-9]+$ ]]
  expected_recovered_count=$((physical_count - 1))
  [ "$recovered_count" -eq "$expected_recovered_count" ]

  recovered_max="$(sqlite3 -batch -noheader "$RECOVERED_DB" 'SELECT max(id) FROM messages NOT INDEXED;')"
  [[ "$recovered_max" =~ ^[0-9]+$ ]]
  [ "$recovered_max" = "$EXPECTED_RECOVERED_MAX_MESSAGE_ID" ]
  sequence_before="$(sqlite3 -batch -noheader "$RECOVERED_DB" \
    "SELECT seq FROM sqlite_sequence WHERE name='messages';")"
  [ "$sequence_before" = "$recovered_max" ]
  calculated_new_id=$((recovered_max + 1))
  [ "$calculated_new_id" = "$EXPECTED_REKEYED_MESSAGE_ID" ]
  new_id="$EXPECTED_REKEYED_MESSAGE_ID"
  work_db_sql=${WORK_DB//\'/\'\'}

  sqlite3 -bail "$RECOVERED_DB" <<SQL
ATTACH '$work_db_sql' AS source_db;
BEGIN IMMEDIATE;
DELETE FROM main.messages WHERE id=$EXPECTED_DUPLICATE_MESSAGE_ID;
INSERT INTO main.messages (
  id,session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,
  token_count,finish_reason,reasoning,reasoning_content,reasoning_details,
  codex_reasoning_items,codex_message_items,platform_message_id,observed,
  active,compacted,effect_disposition,api_content,display_kind,display_metadata
)
SELECT
  CASE WHEN collision_rank=1 THEN $EXPECTED_DUPLICATE_MESSAGE_ID ELSE $new_id END,
  session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,
  token_count,finish_reason,reasoning,reasoning_content,reasoning_details,
  codex_reasoning_items,codex_message_items,platform_message_id,observed,
  active,compacted,effect_disposition,api_content,display_kind,display_metadata
FROM (
  SELECT *,row_number() OVER (ORDER BY timestamp) AS collision_rank
    FROM source_db.messages NOT INDEXED
   WHERE id+0=$EXPECTED_DUPLICATE_MESSAGE_ID
)
ORDER BY timestamp;
COMMIT;
SQL

  post_summary="$(sqlite3 -batch -noheader -separator '|' "$RECOVERED_DB" \
    "SELECT
       (SELECT count(*) FROM messages NOT INDEXED),
       (SELECT timestamp FROM messages NOT INDEXED WHERE id=$EXPECTED_DUPLICATE_MESSAGE_ID),
       (SELECT timestamp FROM messages NOT INDEXED WHERE id=$new_id);")"
  [ "$post_summary" = "$physical_count|$collision_min_timestamp|$collision_max_timestamp" ]
  sequence_after="$(sqlite3 -batch -noheader "$RECOVERED_DB" \
    "SELECT seq FROM sqlite_sequence WHERE name='messages';")"
  [ "$sequence_after" = "$new_id" ]
  expected_full_sha3="$(sqlite3 -batch -noheader "$WORK_DB" \
    "SELECT lower(hex(sha3(group_concat(row_sha3,';'),256)))
       FROM (
         SELECT lower(hex(sha3(json_array(
                  CASE
                    WHEN id+0=$EXPECTED_DUPLICATE_MESSAGE_ID
                     AND printf('%.17g',timestamp)='$EXPECTED_DUPLICATE_LATER_TIMESTAMP'
                    THEN $new_id ELSE id+0
                  END,
                  session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,
                  token_count,finish_reason,reasoning,reasoning_content,reasoning_details,
                  codex_reasoning_items,codex_message_items,platform_message_id,observed,
                  active,compacted,effect_disposition,api_content,display_kind,display_metadata
                ),256))) AS row_sha3,
                CASE
                  WHEN id+0=$EXPECTED_DUPLICATE_MESSAGE_ID
                   AND printf('%.17g',timestamp)='$EXPECTED_DUPLICATE_LATER_TIMESTAMP'
                  THEN $new_id ELSE id+0
                END AS canonical_id
           FROM messages NOT INDEXED
          ORDER BY canonical_id
       );")"
  recovered_full_sha3="$(sqlite3 -batch -noheader "$RECOVERED_DB" \
    "SELECT lower(hex(sha3(group_concat(row_sha3,';'),256)))
       FROM (
         SELECT lower(hex(sha3(json_array(
                  id,session_id,role,content,tool_call_id,tool_calls,tool_name,timestamp,
                  token_count,finish_reason,reasoning,reasoning_content,reasoning_details,
                  codex_reasoning_items,codex_message_items,platform_message_id,observed,
                  active,compacted,effect_disposition,api_content,display_kind,display_metadata
                ),256))) AS row_sha3
           FROM messages NOT INDEXED
          ORDER BY id
       );")"
  [ "$recovered_full_sha3" = "$expected_full_sha3" ]
  log "salvaged duplicate message id=$EXPECTED_DUPLICATE_MESSAGE_ID rekeyed_later_id=$new_id"
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
verify_service_preflight
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
[[ "$BASE_SESSIONS" =~ ^[0-9]+$ ]]
[[ "$BASE_MESSAGES" =~ ^[0-9]+$ ]]
[[ "$BASE_ORPHANS" =~ ^[0-9]+$ ]]
[ "$BASE_SESSIONS" = "$EXPECTED_SESSIONS" ]
[ "$BASE_MESSAGES" = "$EXPECTED_MESSAGES" ]
[ "$BASE_ORPHANS" = "$EXPECTED_ORPHANS" ]
log "approved identity sessions=$BASE_SESSIONS messages=$BASE_MESSAGES orphans=$BASE_ORPHANS"

log "recovering from working copy into isolated database"
sqlite3 "$WORK_DB" '.recover --ignore-freelist' > "$RECOVER_SQL"
chmod 600 "$RECOVER_SQL"
sqlite3 -bail "$RECOVERED_DB" < "$RECOVER_SQL"
verify_non_message_table_parity
salvage_duplicate_message_collision
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
[ "$FTS_COUNTS" = "$BASE_MESSAGES|$BASE_MESSAGES|$BASE_MESSAGES|0|0" ]
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
[ "$POST_FTS" = "$BASE_MESSAGES|$BASE_MESSAGES" ]

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
