#!/usr/bin/env bash
# notify-online.sh — Telegram "online" ping when Hermes gateways start.
#
# Each agent posts a short "online" line to ITS OWN Telegram chat (so Sarp's
# chat gets Sarp's ping, etc). Fired at fleet startup by the launchd agent
# ai.hermes.fleet-online (RunAtLoad). Can also be run by hand for one agent
# after a single-gateway restart.
#
# Usage:
#   notify-online.sh            # all nine
#   notify-online.sh all        # all nine
#   notify-online.sh producer   # just Sarp
#
# Why launchd, not a Hermes hook: Hermes' shell hooks are session/tool/LLM
# scoped (on_session_start fires on a new chat, not on gateway boot). There is
# no gateway-start hook event, so the launchd job that starts the gateway is
# the only reliable "the gateway is up" signal.

set -uo pipefail

TARGET="${1:-all}"
PROFILES_DIR="$HOME/.hermes/profiles"
UID_NUM="$(id -u)"
ORDER="general researcher assistant marketing coder writer producer finance health"

# slug -> display name (persona shown in the chat)
display_name() {
  case "$1" in
    general) echo "Derya" ;;  researcher) echo "Doruk" ;;  assistant) echo "Tuna" ;;
    marketing) echo "Nilay" ;; coder) echo "Naz" ;;        writer) echo "Ozan" ;;
    producer) echo "Sarp" ;;  finance) echo "Murat" ;;     health) echo "Defne" ;;
    *) echo "$1" ;;
  esac
}

getenv() { # slug key -> value (first match, no quotes)
  /usr/bin/grep -h "^$2=" "$PROFILES_DIR/$1/.env" 2>/dev/null | head -1 | cut -d= -f2-
}

gateway_up() { # slug -> 0 if launchd has a live PID for the gateway
  local pid
  pid="$(launchctl list 2>/dev/null | awk -v l="ai.hermes.gateway-$1" '$3==l{print $1}')"
  [ -n "$pid" ] && [ "$pid" != "-" ]
}

wait_gateway() { # slug -> wait up to ~60s for the gateway PID to appear
  for _ in $(seq 1 30); do gateway_up "$1" && return 0; sleep 2; done
  return 1
}

send_one() { # slug
  local slug="$1" tok chat name status text
  tok="$(getenv "$slug" TELEGRAM_BOT_TOKEN)"
  chat="$(getenv "$slug" TELEGRAM_ALLOWED_USERS)"; chat="${chat%%,*}"   # first allowed user = DM chat id
  name="$(display_name "$slug")"
  [ -z "$tok" ]  && { echo "$slug: no TELEGRAM_BOT_TOKEN — skip";  return 1; }
  [ -z "$chat" ] && { echo "$slug: no TELEGRAM_ALLOWED_USERS — skip"; return 1; }

  if wait_gateway "$slug"; then status="🟢"; else status="🟡"; fi
  text="$status $name online — gateway up ($(date '+%H:%M %Z'))"

  if curl -s --max-time 12 "https://api.telegram.org/bot${tok}/sendMessage" \
        --data-urlencode "chat_id=${chat}" \
        --data-urlencode "text=${text}" \
        -d "disable_notification=true" >/dev/null; then
    echo "$slug: sent — ${text}"
  else
    echo "$slug: send FAILED"
  fi
}

if [ "$TARGET" = "all" ]; then
  for s in $ORDER; do [ -d "$PROFILES_DIR/$s" ] && send_one "$s"; done
else
  send_one "$TARGET"
fi
