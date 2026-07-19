#!/usr/bin/env bash
# RETIRED 2026-07-19: this script reads plaintext fleet credentials and fans them
# into profile .env files. The fleet now uses Hermes' native 1Password integration.
# See ../docs/15-credential-management.md. Fail closed so the legacy path cannot be
# used accidentally.
echo "Retired: plaintext bot/provider secret fan-out is disabled. Use docs/15-credential-management.md and hermes secrets onepassword set." >&2
exit 1

# setup-bots.sh — apply name, about text, description, and command menu to the
# nine Hermes Telegram bots via the Bot API.
#
# Bot CREATION is manual (BotFather has no API). This automates everything
# *after* you have the tokens.
#
# Usage:
#   1. In @BotFather: /newbot  x9  (slug-based usernames: general_<you>_bot, researcher_<you>_bot, … health_<you>_bot)
#      Grab each token.
#   2. cp bot-tokens.env.example bot-tokens.env  &&  chmod 600 bot-tokens.env
#      Fill in your numeric Telegram user ID (ALLOWED_USERS, from @userinfobot)
#      and the nine tokens (slug=token per line).
#   3. Optionally fill DEEPSEEK_API_KEY / MINIMAX_API_KEY / OPENROUTER_API_KEY / TINYFISH_API_KEY
#      in bot-tokens.env too — single entry point for all fleet secrets.
#   4. ./setup-bots.sh
#      → sets each bot's profile (name/about/description/commands) AND writes
#        TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS + the provider keys into
#        ~/.hermes/profiles/<slug>/.env
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
  local env="$HOME/.hermes/profiles/$slug/.env"
  upsert_env "$env" TELEGRAM_BOT_TOKEN     "$token"
  upsert_env "$env" TELEGRAM_ALLOWED_USERS "$ALLOWED_USERS"
  echo "  wired $env"
  if curl -s "$API/bot$token/getMe" | grep -q '"ok":true'; then echo "  ok ($slug token valid)"; else echo "  ! WARNING: $slug token failed getMe — bad/revoked token, fix before deploying"; fi
}

setup general   "Derya"  "Derya — studio founder & creative director. Ask anything." \
  "Founder and creative director. Your main line: open conversation, brainstorming, quick answers, hand-offs to the crew. Dry, calm, has seen every pitch."
setup researcher  "Doruk" "Doruk — market analyst. Research, any topic." \
  "The studio's market analyst and scout. Researches any domain — game markets, history, academic. Cites every source, quietly smug. Runs the weekly game scout."
setup assistant "Tuna"  "Tuna — studio manager. Keeps your day running." \
  "Studio manager and the actual adult. Calendar, reminders, your morning digest. Warm, brief, herds the cats so things ship on time."
setup coder     "Naz"   "Naz — lead programmer. Writes & runs code." \
  "Lead programmer. Godot-first game code, refactors, debugging. Blunt, shows diffs, tests her own work. 'Works on my machine' is not a status update."
setup writer    "Ozan"  "Ozan — narrative designer. Drafts & edits." \
  "Narrative designer. Drafts, edits, brainstorms — prose, store copy, game PRDs. Everything's a metaphor, but he delivers. Lean PRDs, brilliant briefly."
setup producer  "Sarp"  "Sarp — producer. Scores game ideas." \
  "Producer and product lead. Holds the budget; scores ideas against the rubric and kills the hype. Skeptical, anti-inflation. Activates in Phase B."
setup marketing "Nilay" "Nilay — marketing & community lead." \
  "Marketing and community lead. Go-to-market: Steam page + wishlists, devlog/social cadence, community, trailer briefs, creator outreach, ASO. Decides what/when/where; briefs Ozan for the words. Honest about reach, no growth-hack fantasies."

# --- Personal tier (non-studio, docs/02 §2.2) ---
setup finance   "Murat" "Murat — markets & finance analyst." \
  "Personal markets & finance analyst. Analyzes read-only data you share (published Google Sheets CSVs, statements, screenshots), scans news/Reddit/finance sites (BIST + global), crunches numbers with code execution. Informational, not investment advice."
setup health    "Defne" "Defne — health & fitness coach." \
  "Personal health & fitness coach. Tracks workouts/nutrition, estimates calories & macros from food photos (ballpark), summarizes trends. Warm, practical. Not a doctor — flags clinical stuff to a professional."

# 3) Fan out model-provider keys (docs/04 §5.8). Empty key = skip; existing
#    values in profile .env files are preserved either way.
ALL_SLUGS=(general researcher assistant coder writer producer marketing finance health)
TINYFISH_SLUGS=("${ALL_SLUGS[@]}")   # all nine — every agent may need web anytime (docs/08)

fanout_key() { # key value slugs…
  local key="$1" val="$2"; shift 2
  [ -n "$val" ] || { echo "skip $key (empty in bot-tokens.env)"; return; }
  for slug in "$@"; do
    upsert_env "$HOME/.hermes/profiles/$slug/.env" "$key" "$val"
  done
  echo "wired $key into: $*"
}

echo
fanout_key DEEPSEEK_API_KEY   "${DEEPSEEK_API_KEY:-}"   "${ALL_SLUGS[@]}"
fanout_key MINIMAX_API_KEY    "${MINIMAX_API_KEY:-}"    "${ALL_SLUGS[@]}"
fanout_key OPENROUTER_API_KEY "${OPENROUTER_API_KEY:-}" "${ALL_SLUGS[@]}"
fanout_key TINYFISH_API_KEY   "${TINYFISH_API_KEY:-}"   "${TINYFISH_SLUGS[@]}"

echo
echo "Done. Bot profiles set; tokens + keys wired into ~/.hermes/profiles/<slug>/.env."
echo "Two settings still need BotFather (per bot): /setprivacy Disable, /setjoingroups Disable."
