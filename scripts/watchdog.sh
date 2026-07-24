#!/bin/bash
# Watchdog — checks gateway health every five minutes.
# Tracks PID changes and notifies Telegram only when state changes.

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

# Honcho stack containers (OrbStack) — memory backend for the whole fleet.
# Alert-only: containers are unless-stopped, so if OrbStack itself is down
# a start attempt is pointless; the human needs to know either way.
DOCKER="${HERMES_WATCHDOG_DOCKER:-$HOME/.orbstack/bin/docker}"
HONCHO_CONTAINERS="server-api-1 server-database-1 server-redis-1 server-deriver-1"
HONCHO_DOWN=""
if [ -x "$DOCKER" ]; then
    for c in $HONCHO_CONTAINERS; do
        state=$("$DOCKER" inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
        [ "$state" = "true" ] || HONCHO_DOWN="$HONCHO_DOWN $c"
    done
else
    HONCHO_DOWN=" inspector-unavailable"
fi
if [ -n "$HONCHO_DOWN" ]; then
    ISSUES="$ISSUES\n  - Honcho container(s) down:$HONCHO_DOWN (OrbStack?)"
    ALL_OK=false
    DOWN_COUNT=$((DOWN_COUNT+1))
fi

_asm() {
    case "$1" in
        general)    echo "Derya" ;;
        assistant)  echo "Tuna" ;;
        coder)      echo "Naz" ;;
        finance)    echo "Murat" ;;
        health)     echo "Defne" ;;
        marketing)  echo "Nilay" ;;
        producer)   echo "Sarp" ;;
        researcher) echo "Doruk" ;;
        writer)     echo "Ozan" ;;
    esac
}

# Save current PIDs
> "$CURRENT_FILE"
for p in $PROFILES; do
    LABEL="ai.hermes.gateway-$p"
    OUT=$(launchctl list "$LABEL" 2>/dev/null)
    PID=$(echo "$OUT" | grep -o '"PID"[[:space:]]*=[[:space:]]*[0-9]*' | head -1 | grep -o '[0-9]*$')
    EXIT_CODE=$(echo "$OUT" | grep -o '"LastExitStatus"[[:space:]]*=[[:space:]]*-*[0-9]*' | head -1 | grep -o -- '-*[0-9]*$')
    PROCESS_RUNNING=$(ps -p "$PID" -o pid= 2>/dev/null | tr -d ' ')
    echo "$p:$PID:$EXIT_CODE" >> "$CURRENT_FILE"

    if [ -n "$PID" ] && [ -z "$PROCESS_RUNNING" ]; then
        ISSUES="$ISSUES\n  - $p has stale PID $PID (exit: ${EXIT_CODE:-?})"
        ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
    elif [ -z "$PID" ]; then
        ISSUES="$ISSUES\n  - $p not running (exit: ${EXIT_CODE:-not-loaded})"
        ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
    fi
done

# Compare with previous state to detect PID changes
if [ -f "$PREV_FILE" ]; then
    while IFS=: read -r profile old_pid old_exit; do
        new_entry=$(grep "^$profile:" "$CURRENT_FILE" | head -1)
        new_pid=$(echo "$new_entry" | cut -d: -f2)
        display=$(_asm "$profile")

        if [ -n "$old_pid" ] && [ -n "$new_pid" ] && [ "$old_pid" != "$new_pid" ]; then
            REPORT="$REPORT\n  - $display ($profile): PID $old_pid -> $new_pid"
            RESTART_COUNT=$((RESTART_COUNT+1))
        fi
    done < "$PREV_FILE"
fi

# Build message with actual newlines
MESSAGE=""
if [ -n "$REPORT" ] || [ -n "$ISSUES" ]; then
    MESSAGE="\n"
    MESSAGE="${MESSAGE}Watchdog - $NOW\n"
    MESSAGE="${MESSAGE}---\n"
    if [ -n "$REPORT" ]; then
        MESSAGE="${MESSAGE}\nRestarted:$REPORT"
    fi
    if [ -n "$ISSUES" ]; then
        MESSAGE="${MESSAGE}\nCrashed:$ISSUES"
    fi
    MESSAGE="${MESSAGE}\n---\n"
    if [ "$ALL_OK" = true ] && [ "$RESTART_COUNT" -gt 0 ]; then
        MESSAGE="${MESSAGE}All healthy | $RESTART_COUNT restart(s)"
    elif [ "$DOWN_COUNT" -gt 0 ]; then
        MESSAGE="${MESSAGE}$DOWN_COUNT component failure(s)"
    fi
fi

# Send if state changed
if [ -n "$MESSAGE" ]; then
    NEW_STATE=$(printf "%s" "$REPORT|$ISSUES|$ALL_OK|$DOWN_COUNT|$RESTART_COUNT" | md5)
    LAST_STATE=""
    [ -f "$STATUS_FILE" ] && LAST_STATE=$(cat "$STATUS_FILE")
    
    if [ "$LAST_STATE" != "$NEW_STATE" ]; then
        echo "[$NOW_LOG] $MESSAGE" >> "$LOG_FILE"
        if printf "%b" "$MESSAGE" | "$HERMES_SEND" general --to telegram --file -; then
            echo "$NEW_STATE" > "$STATUS_FILE"
            cp "$CURRENT_FILE" "$PREV_FILE"
        else
            echo "[$NOW_LOG] alert delivery failed" >> "$LOG_FILE"
            exit 1
        fi
    fi
elif [ "$ALL_OK" = true ]; then
    echo "OK" > "$STATUS_FILE"
    cp "$CURRENT_FILE" "$PREV_FILE"
fi
