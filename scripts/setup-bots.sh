#!/usr/bin/env bash
# setup-bots.sh — apply name, about text, description, and command menu to the
# seven Hermes Telegram bots via the Bot API.
#
# Bot CREATION is manual (BotFather has no API). This automates everything
# *after* you have the tokens.
#
# Usage:
#   1. In @BotFather: /newbot  x7  (usernames: kam_<you>_bot, mergen_<you>_bot, …)
#      Grab each token.
#   2. cp bot-tokens.env.example bot-tokens.env  &&  chmod 600 bot-tokens.env
#      Fill in your numeric Telegram user ID (ALLOWED_USERS, from @userinfobot)
#      and the seven tokens (slug=token per line).
#   3. ./setup-bots.sh
#      → sets each bot's profile (name/about/description/commands) AND writes
#        TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS into ~/.hermes-<slug>/.env
#        (chmod 600, non-destructive — other keys in the file are preserved).
#
# Still manual afterward (BotFather-only, ~10s each):
#   /mybots → pick bot → Bot Settings → /setprivacy   → Disable
#                                       /setjoingroups → Disable

set -euo pipefail
cd "$(dirname "$0")"

[ -f bot-tokens.env ] || { echo "Missing bot-tokens.env (copy bot-tokens.env.example)"; exit 1; }
# shellcheck disable=SC1091
source ./bot-tokens.env
ALLOWED_USERS="${ALLOWED_USERS:-}"
[ -n "$ALLOWED_USERS" ] || echo "WARNING: ALLOWED_USERS empty — bots will deny all messages until it's set (get it from @userinfobot)."

API="https://api.telegram.org"

call() { # token method json
  local token="$1" method="$2" data="$3" resp
  resp=$(curl -s -X POST "$API/bot$token/$method" \
    -H 'Content-Type: application/json' -d "$data")
  [[ "$resp" == *'"ok":true'* ]] || { echo "  ! $method failed: $resp"; return 1; }
}

commands='{"commands":[
  {"command":"new","description":"Start a fresh conversation"},
  {"command":"status","description":"Show session info"},
  {"command":"stop","description":"Stop the current task"},
  {"command":"help","description":"Show available commands"}]}'

upsert_env() { # file key value — replace the key's line (or append), preserve the rest
  local file="$1" key="$2" val="$3" rest
  mkdir -p "$(dirname "$file")"; touch "$file"
  rest="$(grep -v "^${key}=" "$file" || true)"
  { [ -n "$rest" ] && printf '%s\n' "$rest"; printf '%s=%s\n' "$key" "$val"; } > "$file"
  chmod 600 "$file"
}

setup() { # slug name about description
  local slug="$1" name="$2" about="$3" desc="$4" token="${!1:-}"
  if [ -z "$token" ]; then echo "skip $slug (no token in bot-tokens.env)"; return; fi
  echo "Configuring $slug ($name)…"
  # 1) Bot API profile
  call "$token" setMyName             "{\"name\":\"$name\"}"            || true
  call "$token" setMyShortDescription "{\"short_description\":\"$about\"}" || true
  call "$token" setMyDescription      "{\"description\":\"$desc\"}"     || true
  call "$token" setMyCommands         "$commands"                       || true
  # 2) Wire token + allowed-user into the agent's data-dir .env (non-destructive)
  local env="$HOME/.hermes-$slug/.env"
  upsert_env "$env" TELEGRAM_BOT_TOKEN     "$token"
  upsert_env "$env" TELEGRAM_ALLOWED_USERS "$ALLOWED_USERS"
  echo "  wired $env"
  if curl -s "$API/bot$token/getMe" | grep -q '"ok":true'; then echo "  ok"; fi
}

setup general   "Kam"    "Kam Ata — your shaman. Ask anything." \
  "Father Shaman. Your main line: open conversation, brainstorming, quick answers, and hand-offs to the specialists. Talk about anything."
setup research  "Mergen" "Mergen Han — research, any topic." \
  "Lord of wisdom. Researches any domain — game markets, history, academic sources. Cites every source. Runs the weekly game scout."
setup concierge "Umay"   "Umay Ana — daily life & digests." \
  "Mother of the hearth. Calendar, reminders, and your morning digest. Warm, brief, action-first."
setup ops       "Asena"  "Asena Ana — watches the system." \
  "The wolf-mother, ever vigilant. Monitors the agent fleet and host, sends status reports, runs scheduled checks. Terse and factual."
setup coder     "Ülgen"  "Bay Ülgen — writes & runs code." \
  "The maker. Your development pair: Godot-first game code, refactors, debugging. Direct, shows diffs, tests its own work."
setup writer    "Korkut" "Dede Korkut — drafts & edits." \
  "The legendary bard. Drafts, edits, brainstorms — prose, store copy, game PRDs. Playful and generative."
setup producer  "Kayra"  "Kayra Han — scores game ideas." \
  "The creator. Game-dev discovery: keeps the opportunity backlog and scores ideas against the rubric. Activates in Phase B."

echo
echo "Done. Profiles set + tokens wired into ~/.hermes-<slug>/.env."
echo "Two settings still need BotFather (per bot): /setprivacy Disable, /setjoingroups Disable."
