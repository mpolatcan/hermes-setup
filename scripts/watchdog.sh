#!/bin/bash
# Fleet watchdog: process, dependency, endpoint and backup freshness checks.

STATE_DIR="${HERMES_WATCHDOG_STATE_DIR:-/tmp}"
mkdir -p "$STATE_DIR"
STATUS_FILE="$STATE_DIR/hermes-watchdog-last"
PREV_FILE="$STATE_DIR/hermes-watchdog-prev"
CURRENT_FILE="$STATE_DIR/hermes-watchdog-current"
LOG_FILE="$STATE_DIR/hermes-watchdog.log"
HERMES_SEND="${HERMES_WATCHDOG_SEND:-/Users/mutlupolatcan/.hermes/scripts/hermes-send-keychain.sh}"
NOW=$(date "+%H:%M")
NOW_LOG=$(date "+%Y-%m-%d %H:%M:%S")
PROFILES="general assistant coder finance health marketing producer researcher writer"
RESTART_COUNT=0
DOWN_COUNT=0
ALL_OK=true
REPORT=""
ISSUES=""

DOCKER="${HERMES_WATCHDOG_DOCKER:-$HOME/.orbstack/bin/docker}"
HONCHO_CONTAINERS="server-api-1 server-database-1 server-redis-1 server-deriver-1"
HONCHO_DOWN=""
if [ -x "$DOCKER" ]; then
    for c in $HONCHO_CONTAINERS; do
        state=$("$DOCKER" inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
        health=$("$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null)
        [ "$state" = "true" ] || HONCHO_DOWN="$HONCHO_DOWN $c:not-running"
        [ "$health" = "healthy" ] || HONCHO_DOWN="$HONCHO_DOWN $c:$health"
    done
else
    HONCHO_DOWN=" inspector-unavailable"
fi
if [ -n "$HONCHO_DOWN" ]; then
    ISSUES="$ISSUES\n  - Honcho unhealthy:$HONCHO_DOWN"
    ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
fi

endpoint_ok() {
    python3 - "$1" <<'PY'
import json,sys,urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    if response.status != 200: raise SystemExit(1)
    body=json.loads(response.read())
    if body.get('status') != 'ok': raise SystemExit(1)
PY
}
endpoint_ok "http://127.0.0.1:8000/health" || {
    ISSUES="$ISSUES\n  - Honcho API health endpoint failed"; ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1));
}
endpoint_ok "http://127.0.0.1:18080/healthz" || {
    ISSUES="$ISSUES\n  - Honcho Codex adapter health endpoint failed"; ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1));
}

QUEUE_PROBE="${HERMES_WATCHDOG_QUEUE_PROBE:-/Users/mutlupolatcan/.hermes/services/honcho-stack/probe-honcho-queue.py}"
QUEUE_OUTPUT=$("$QUEUE_PROBE" 2>/dev/null)
QUEUE_RC=$?
if [ "$QUEUE_RC" -eq 2 ]; then
    QUEUE_COUNTS=$(printf '%s' "$QUEUE_OUTPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("pending=%s, in_progress=%s" % (d.get("pending",0),d.get("in_progress",0)))' 2>/dev/null)
    ISSUES="$ISSUES\n  - Honcho queue threshold exceeded: ${QUEUE_COUNTS:-counts-unavailable}"
    ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
elif [ "$QUEUE_RC" -ne 0 ]; then
    ISSUES="$ISSUES\n  - Honcho authenticated queue probe failed"
    ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
fi

fresh_backup() {
    python3 - "$1" "$2" "$3" <<'PY'
from pathlib import Path
import sys,time
root,pattern,max_age=sys.argv[1],sys.argv[2],int(sys.argv[3])
items=list(Path(root).glob(pattern))
if not items: raise SystemExit(1)
latest=max(items,key=lambda p:p.stat().st_mtime)
if time.time()-latest.stat().st_mtime > max_age: raise SystemExit(1)
PY
}
fresh_backup "/Users/mutlupolatcan/.hermes/backups/honcho" "honcho-*.sql.gz" 57600 || {
    ISSUES="$ISSUES\n  - Honcho backup missing or older than 16h"; ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1));
}
fresh_profile_snapshots() {
    python3 - 93600 $PROFILES <<'PY'
from pathlib import Path
import json, sys, time
max_age = int(sys.argv[1])
root = Path("/Users/mutlupolatcan/.hermes/profiles")
for profile in sys.argv[2:]:
    snapshots = [p for p in (root / profile / "state-snapshots").glob("*") if p.is_dir()]
    if not snapshots:
        raise SystemExit(1)
    latest = max(snapshots, key=lambda p: p.stat().st_mtime)
    if time.time() - latest.stat().st_mtime > max_age:
        raise SystemExit(1)
    manifest = latest / "manifest.json"
    if not manifest.is_file():
        raise SystemExit(1)
    data = json.loads(manifest.read_text())
    if not isinstance(data, dict):
        raise SystemExit(1)
PY
}
fresh_profile_snapshots || {
    ISSUES="$ISSUES\n  - Hermes profile snapshots missing, invalid or older than 26h"; ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1));
}

_asm() {
    case "$1" in
        general) echo "Derya";; assistant) echo "Tuna";; coder) echo "Naz";; finance) echo "Murat";;
        health) echo "Defne";; marketing) echo "Nilay";; producer) echo "Sarp";; researcher) echo "Doruk";; writer) echo "Ozan";;
    esac
}

> "$CURRENT_FILE"
for p in $PROFILES; do
    LABEL="ai.hermes.gateway-$p"
    OUT=$(launchctl list "$LABEL" 2>/dev/null)
    PID=$(echo "$OUT" | grep -o '"PID"[[:space:]]*=[[:space:]]*[0-9]*' | head -1 | grep -o '[0-9]*$')
    EXIT_CODE=$(echo "$OUT" | grep -o '"LastExitStatus"[[:space:]]*=[[:space:]]*-*[0-9]*' | head -1 | grep -o -- '-*[0-9]*$')
    PROCESS_RUNNING=$(ps -p "$PID" -o pid= 2>/dev/null | tr -d ' ')
    echo "$p:$PID:$EXIT_CODE" >> "$CURRENT_FILE"
    if [ -n "$PID" ] && [ -z "$PROCESS_RUNNING" ]; then
        ISSUES="$ISSUES\n  - $p has stale PID $PID (exit: ${EXIT_CODE:-?})"; ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
    elif [ -z "$PID" ]; then
        ISSUES="$ISSUES\n  - $p not running (exit: ${EXIT_CODE:-not-loaded})"; ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
    fi
done

if [ -f "$PREV_FILE" ]; then
    while IFS=: read -r profile old_pid old_exit; do
        new_entry=$(grep "^$profile:" "$CURRENT_FILE" | head -1)
        new_pid=$(echo "$new_entry" | cut -d: -f2)
        display=$(_asm "$profile")
        if [ -n "$old_pid" ] && [ -n "$new_pid" ] && [ "$old_pid" != "$new_pid" ]; then
            REPORT="$REPORT\n  - $display ($profile): PID $old_pid -> $new_pid"; RESTART_COUNT=$((RESTART_COUNT+1))
        fi
    done < "$PREV_FILE"
fi

MESSAGE=""
RECOVERY=false
if [ -n "$REPORT" ] || [ -n "$ISSUES" ]; then
    MESSAGE="\nWatchdog - $NOW\n---\n"
    [ -z "$REPORT" ] || MESSAGE="${MESSAGE}\nRestarted:$REPORT"
    [ -z "$ISSUES" ] || MESSAGE="${MESSAGE}\nFailed:$ISSUES"
    MESSAGE="${MESSAGE}\n---\n"
    if [ "$ALL_OK" = true ] && [ "$RESTART_COUNT" -gt 0 ]; then MESSAGE="${MESSAGE}All healthy | $RESTART_COUNT restart(s)";
    elif [ "$DOWN_COUNT" -gt 0 ]; then MESSAGE="${MESSAGE}$DOWN_COUNT component failure(s)"; fi
elif [ "$ALL_OK" = true ] && [ -f "$STATUS_FILE" ] && [ "$(cat "$STATUS_FILE")" != "OK" ]; then
    MESSAGE="\nWatchdog - $NOW\n---\nRecovered: all monitored components healthy\n---\n"
    RECOVERY=true
fi

if [ -n "$MESSAGE" ]; then
    NEW_STATE=$(printf "%s" "$REPORT|$ISSUES|$ALL_OK|$DOWN_COUNT|$RESTART_COUNT" | md5)
    LAST_STATE=""; [ -f "$STATUS_FILE" ] && LAST_STATE=$(cat "$STATUS_FILE")
    if [ "$LAST_STATE" != "$NEW_STATE" ]; then
        echo "[$NOW_LOG] $MESSAGE" >> "$LOG_FILE"
        if printf "%b" "$MESSAGE" | "$HERMES_SEND" general --to telegram --file -; then
            if [ "$RECOVERY" = true ]; then echo "OK" > "$STATUS_FILE"; else echo "$NEW_STATE" > "$STATUS_FILE"; fi
            cp "$CURRENT_FILE" "$PREV_FILE"
        else
            echo "[$NOW_LOG] alert delivery failed" >> "$LOG_FILE"; exit 1
        fi
    fi
elif [ "$ALL_OK" = true ]; then
    echo "OK" > "$STATUS_FILE"; cp "$CURRENT_FILE" "$PREV_FILE"
fi
