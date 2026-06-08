#!/usr/bin/env bash
# wire-tinyfish.sh — give EVERY profile the same web stack:
#   TinyFish (primary, via MCP / OAuth)  +  SearXNG (fallback, via built-in `web`).
#
# Verified on the Mini (v0.16.0): TinyFish's MCP endpoint uses OAuth 2.1 PKCE,
# NOT an X-API-Key header. Hermes stores the server as `mcp_servers.tinyfish`
# (auth: oauth) in config.yaml and keeps the OAuth token triplet under
# <profile>/mcp-tokens/.
#
# OAuth tokens are NOT shareable across profiles. The endpoint uses per-client
# Dynamic Client Registration + refresh-token rotation: every profile gets its
# own client_id and its own token family. COPYING a triplet between profiles
# gives them one shared client_id + one shared refresh token; the first gateway
# to refresh rotates that token and the others are left holding a revoked one,
# and reuse-detection then invalidates the WHOLE family. A copy "works" for ~1h
# (the access token is still alive) and then dies — that is the trap this script
# used to fall into. So each profile authenticates ITSELF via its own browser
# consent; there is no seeding.
#
# Per profile it:
#   1. upserts SEARXNG_URL into .env
#   2. writes the mcp_servers.tinyfish block to config.yaml (if absent)
#   3. writes the web: { backend: searxng } fallback block (if absent)
#   4. removes `web` from disabled_toolsets so the SearXNG fallback can fire
#      (handles inline `[...]` and multi-line `- web` forms; .bak saved)
#   5. runs `hermes -p <slug> mcp add tinyfish` if the profile has no token
#      (opens a browser consent on the Mini — one per profile)
#   6. kickstarts the gateway, then you verify with `hermes -p <p> mcp test tinyfish`
#
# Config wiring is idempotent + non-destructive. OAuth is auto-launched for any
# profile that lacks a token. To repair profiles that hold a BAD copied token,
# pass FRESH=1 to wipe their mcp-tokens/ first so they re-auth clean.
#
# Usage:
#   ./wire-tinyfish.sh                                   # wire all nine; auth any missing
#   FRESH=1 SLUGS="general researcher assistant coder writer producer" ./wire-tinyfish.sh
#                                                        # ^ repair the 6 copied-token profiles
#   AUTH=0 ./wire-tinyfish.sh                            # wire config only; just PRINT auth cmds
#   SEARXNG_URL=http://127.0.0.1:8888 ./wire-tinyfish.sh # override fallback URL

set -euo pipefail
cd "$(dirname "$0")"

SEARXNG_URL="${SEARXNG_URL:-http://127.0.0.1:8888}"
SLUGS="${SLUGS:-general researcher assistant marketing coder writer producer finance health}"
AUTH="${AUTH:-1}"     # 1 = launch the OAuth flow; 0 = just print the command
FRESH="${FRESH:-0}"   # 1 = wipe each profile's mcp-tokens/ first (repair copied tokens)

# Preflight: SearXNG must answer JSON or the fallback is dead weight.
if ! curl -s --max-time 3 "$SEARXNG_URL/search?q=ping&format=json" 2>/dev/null | grep -q '"results"'; then
  echo "WARNING: SearXNG not returning JSON at $SEARXNG_URL — fallback won't fire (docs/08 §10.4)."
  echo
fi

upsert_env() { # file key value
  local file="$1" key="$2" val="$3" rest
  mkdir -p "$(dirname "$file")"; touch "$file"
  rest="$(grep -v "^${key}=" "$file" || true)"
  { [ -n "$rest" ] && printf '%s\n' "$rest"; printf '%s=%s\n' "$key" "$val"; } > "$file"
  chmod 600 "$file"
}

read -r -d '' MCP_BLOCK <<'YAML' || true

# --- TinyFish web search/fetch via MCP — primary (OAuth 2.1 PKCE, docs/08) ---
mcp_servers:
  tinyfish:
    url: https://agent.tinyfish.ai/mcp
    auth: oauth
    enabled: true
YAML

read -r -d '' WEB_BLOCK <<'YAML' || true

# --- web fallback: SearXNG (TinyFish stays primary via MCP) ---
web:
  backend: searxng
  search_backend: searxng
YAML

restart=()
for slug in $SLUGS; do
  dir="$HOME/.hermes/profiles/$slug"
  cfg="$dir/config.yaml"
  if [ ! -d "$dir" ]; then echo "skip $slug (no profile dir)"; continue; fi

  echo "$slug:"
  upsert_env "$dir/.env" SEARXNG_URL "$SEARXNG_URL"

  if [ ! -f "$cfg" ]; then
    echo "    ! no config.yaml — run 'hermes -p $slug setup' first."; continue
  fi

  # 1) TinyFish MCP server (primary)
  if grep -q 'tinyfish' "$cfg"; then
    echo "    = tinyfish MCP server already in config"
  elif grep -qE '^mcp_servers:' "$cfg"; then
    echo "    ! an 'mcp_servers:' block exists without tinyfish — add by hand (no auto-merge)."
  else
    printf '%s\n' "$MCP_BLOCK" >> "$cfg"
    echo "    + mcp_servers.tinyfish block appended"
  fi

  # 2) SearXNG web backend (fallback)
  if grep -qE '^web:' "$cfg"; then
    echo "    = web: block already present"
  else
    printf '%s\n' "$WEB_BLOCK" >> "$cfg"
    echo "    + web: searxng fallback block appended"
  fi

  # 3) Ensure built-in web toolset enabled (strip `web` from disabled_toolsets)
  if grep -qE 'disabled_toolsets:.*\bweb\b' "$cfg"; then
    sed -i.bak -E \
      -e '/disabled_toolsets:/ s/\[web, /[/' \
      -e '/disabled_toolsets:/ s/, web\]/]/' \
      -e '/disabled_toolsets:/ s/, web,/,/' \
      -e '/disabled_toolsets:/ s/\[web\]/[]/' "$cfg"
    echo "    + web removed from disabled_toolsets (inline; .bak saved)"
  elif grep -qE '^[[:space:]]*-[[:space:]]*web[[:space:]]*$' "$cfg"; then
    sed -i.bak -E '/^[[:space:]]*-[[:space:]]*web[[:space:]]*$/d' "$cfg"
    echo "    + web removed from disabled_toolsets (multi-line; .bak saved)"
  fi

  # 4) OAuth — each profile authenticates ITSELF (tokens are NOT shareable).
  tok="$dir/mcp-tokens"
  if [ "$FRESH" = "1" ] && [ -d "$tok" ]; then
    rm -rf "$tok"
    echo "    + wiped mcp-tokens/ (FRESH=1) — will re-auth clean"
  fi
  if [ -f "$tok/tinyfish.json" ]; then
    echo "    = OAuth tokens already present (pass FRESH=1 to force re-auth)"
  elif [ "$AUTH" = "1" ]; then
    echo "    → launching OAuth flow (browser consent on the Mini)…"
    if hermes -p "$slug" mcp add tinyfish --url https://agent.tinyfish.ai/mcp; then
      echo "    + $slug authenticated"
    else
      echo "    ! OAuth failed for $slug — re-run: hermes -p $slug mcp add tinyfish --url https://agent.tinyfish.ai/mcp"
    fi
  else
    echo "    ! no OAuth tokens (AUTH=0) — run: hermes -p $slug mcp add tinyfish --url https://agent.tinyfish.ai/mcp"
  fi

  restart+=("$slug")
done

echo
if [ ${#restart[@]} -gt 0 ]; then
  echo "Restarting gateways: ${restart[*]}"
  for slug in "${restart[@]}"; do
    launchctl kickstart -k "gui/$(id -u)/ai.hermes.gateway-$slug" 2>/dev/null \
      || echo "  ! $slug: kickstart failed (gateway not installed — on-demand agent?)"
  done
fi
echo
echo "Done. Verify per agent:  hermes -p general mcp test tinyfish   (expect: ✓ Connected, 17 tools)"
