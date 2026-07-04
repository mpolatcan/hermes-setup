#!/bin/bash
# Watchdog — 5 dk'da bir gateway sağlığını kontrol eder
# PID değişimlerini takip eder, sadece state değişiminde Telegram'a bildirim

STATUS_FILE="/tmp/hermes-watchdog-last"
PREV_FILE="/tmp/hermes-watchdog-prev"
LOG_FILE="/tmp/hermes-watchdog.log"
HERMES="/opt/homebrew/bin/hermes"
NOW=$(date "+%H:%M")
NOW_LOG=$(date "+%Y-%m-%d %H:%M:%S")

PROFILES="general assistant coder finance health marketing producer researcher writer"

RESTART_COUNT=0
DOWN_COUNT=0
ALL_OK=true

# Honcho stack containers (OrbStack) — memory backend for the whole fleet.
# Alert-only: containers are unless-stopped, so if OrbStack itself is down
# a start attempt is pointless; the human needs to know either way.
DOCKER="$HOME/.orbstack/bin/docker"
HONCHO_CONTAINERS="server-api-1 server-database-1 server-redis-1 server-deriver-1"
HONCHO_DOWN=""
if [ -x "$DOCKER" ]; then
    for c in $HONCHO_CONTAINERS; do
        state=$("$DOCKER" inspect -f '{{.State.Running}}' "$c" 2>/dev/null)
        [ "$state" = "true" ] || HONCHO_DOWN="$HONCHO_DOWN $c"
    done
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
> /tmp/hermes-watchdog-current
for p in $PROFILES; do
    LABEL="ai.hermes.gateway-$p"
    OUT=$(launchctl list "$LABEL" 2>/dev/null)
    PID=$(echo "$OUT" | grep -o '"PID"[[:space:]]*=[[:space:]]*[0-9]*' | head -1 | grep -o '[0-9]*$')
    EXIT_CODE=$(echo "$OUT" | grep -o '"LastExitStatus"[[:space:]]*=[[:space:]]*-*[0-9]*' | head -1 | grep -o -- '-*[0-9]*$')
    PROCESS_RUNNING=$(ps -p "$PID" -o pid= 2>/dev/null | tr -d ' ')
    echo "$p:$PID:$EXIT_CODE" >> /tmp/hermes-watchdog-current

    if [ -n "$PID" ] && [ -z "$PROCESS_RUNNING" ] && [ -n "$EXIT_CODE" ] && [ "$EXIT_CODE" != "0" ]; then
        ISSUES="$ISSUES\n  - $p crashed (exit: $EXIT_CODE)"
        ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
    elif [ -z "$PID" ] && [ -n "$EXIT_CODE" ] && [ "$EXIT_CODE" != "0" ]; then
        ISSUES="$ISSUES\n  - $p exited (exit: $EXIT_CODE)"
        ALL_OK=false; DOWN_COUNT=$((DOWN_COUNT+1))
    fi
done

# Compare with previous state to detect PID changes
if [ -f "$PREV_FILE" ]; then
    while IFS=: read -r profile old_pid old_exit; do
        new_entry=$(grep "^$profile:" /tmp/hermes-watchdog-current | head -1)
        new_pid=$(echo "$new_entry" | cut -d: -f2)
        new_exit=$(echo "$new_entry" | cut -d: -f3)
        display=$(_asm "$profile")

        if [ -n "$old_pid" ] && [ -n "$new_pid" ] && [ "$old_pid" != "$new_pid" ]; then
            REPORT="$REPORT\n  - $display ($profile): PID $old_pid -> $new_pid"
            RESTART_COUNT=$((RESTART_COUNT+1))
        elif [ -n "$old_pid" ] && [ -z "$new_pid" ] && [ -z "$(ps -p $old_pid -o pid= 2>/dev/null | tr -d ' ')" ]; then
            REPORT="$REPORT\n  - $display ($profile): down (PID: $old_pid, exit: ${new_exit:-?})"
            DOWN_COUNT=$((DOWN_COUNT+1))
            ALL_OK=false
        fi
    done < "$PREV_FILE"
fi

cp /tmp/hermes-watchdog-current "$PREV_FILE"

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
        MESSAGE="${MESSAGE}$DOWN_COUNT profile(s) down"
    fi
fi

# Send if state changed
if [ -n "$MESSAGE" ]; then
    NEW_STATE=$(echo "$MESSAGE" | md5)
    LAST_STATE=""
    [ -f "$STATUS_FILE" ] && LAST_STATE=$(cat "$STATUS_FILE")
    
    if [ "$LAST_STATE" != "$NEW_STATE" ]; then
        echo "[$NOW_LOG] $MESSAGE" >> "$LOG_FILE"
        echo "$NEW_STATE" > "$STATUS_FILE"
        printf "%b" "$MESSAGE" | $HERMES -p general send --to telegram -
    fi
elif [ "$ALL_OK" = true ]; then
    echo "OK" > "$STATUS_FILE"
fi
