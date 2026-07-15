#!/bin/bash
# Fleet online notification — runs at login via launchd RunAtLoad
PROFILES="general assistant coder finance health marketing producer researcher writer"
HERMES="/opt/homebrew/bin/hermes"
LOG_FILE="/tmp/hermes-fleet-online.log"

sleep 15  # wait for all gateways to start

ONLINE=""
OFFLINE=""
for p in $PROFILES; do
    LABEL="ai.hermes.gateway-$p"
    PID=$(launchctl list "$LABEL" 2>/dev/null | grep -o '"PID"[[:space:]]*=[[:space:]]*[0-9]*' | head -1 | grep -o '[0-9]*$')
    RUNNING=$(ps -p "$PID" -o pid= 2>/dev/null | tr -d ' ')
    if [ -n "$RUNNING" ]; then
        ONLINE="$ONLINE $p"
    else
        OFFLINE="$OFFLINE $p"
    fi
done

TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")
echo "[$TIMESTAMP] Online:$ONLINE | Offline:$OFFLINE" >> "$LOG_FILE"

if [ -z "$OFFLINE" ]; then
    $HERMES -p general send --to telegram "🚀 **Fleet ready:** all 9 profile gateways are running ✅"
else
    $HERMES -p general send --to telegram "⚠️ **Fleet boot:** Offline:$OFFLINE — check required"
fi
