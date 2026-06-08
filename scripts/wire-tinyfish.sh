#!/usr/bin/env bash
# wire-tinyfish.sh — give EVERY profile the same web stack:
#   TinyFish (primary, via MCP / OAuth)  +  SearXNG (fallback, via built-in `web`).
#
# Verified on the Mini (v0.16.0): TinyFish's MCP endpoint uses OAuth 2.1 PKCE,
# NOT an X-API-Key header. Hermes stores the server as `mcp_servers.tinyfish`
# (auth: oauth) in config.yaml and keeps the OAuth token triplet under
# <profile>/mcp-tokens/. The browser OAuth flow can't be scripted, so this tool
# *seeds* the tokens from one already-authed profile (default: researcher) into
# the rest — single-user box, so sharing one TinyFish OAuth client is fine.
#
# Per profile it:
#   1. upserts SEARXNG_URL into .env
#   2. writes the mcp_servers.tinyfish block to config.yaml (if absent)
#   3. writes the web: { backend: searxng } fallback block (if absent)
#   4. removes `web` from disabled_toolsets so the SearXNG fallback can fire
#      (handles inline `[...]` and multi-line `- web` forms; .bak saved)
#   5. seeds the OAuth token triplet from $SEED_FROM (if the profile has none)
#   6. kickstarts the gateway, then you verify with `hermes -p <p> mcp test tinyfish`
#
# Idempotent + non-destructive. First authenticate ONE profile by hand:
#   hermes -p researcher mcp add tinyfish --url https://agent.tinyfish.ai/mcp
# then run this to fan it out.
#
# Usage:
#   ./wire-tinyfish.sh                                   # seed from researcher
#   SEED_FROM=marketing ./wire-tinyfish.sh               # seed from another authed profile
#   SLUGS="general producer" ./wire-tinyfish.sh          # subset (default: all nine)
#   SEARXNG_URL=http://127.0.0.1:8888 ./wire-tinyfish.sh # override fallback URL

set -euo pipefail
cd "$(dirname "$0")"

SEED_FROM="${SEED_FROM:-researcher}"
SEARXNG_URL="${SEARXNG_URL:-http://127.0.0.1:8888}"
SLUGS="${SLUGS:-general researcher assistant marketing coder writer producer finance health}"
SEED_DIR="$HOME/.hermes/profiles/$SEED_FROM/mcp-tokens"

# Preflight: SearXNG must answer JSON or the fallback is dead weight.
if ! curl -s --max-time 3 "$SEARXNG_URL/search?q=ping&format=json" 2>/dev/null | grep -q '"results"'; then
  echo "WARNING: SearXNG not returning JSON at $SEARXNG_URL — fallback won't fire (docs/08 §10.4)."
  echo
fi
# Preflight: seed profile must be OAuth-authed, or there's nothing to fan out.
if [ ! -f "$SEED_DIR/tinyfish.json" ]; then
  echo "NOTE: seed profile '$SEED_FROM' has no TinyFish OAuth token yet."
  echo "      Authenticate it first:  hermes -p $SEED_FROM mcp add tinyfish --url https://agent.tinyfish.ai/mcp"
  echo "      (config + fallback will still be wired; tokens just won't be seeded.)"
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

  # 4) Seed OAuth tokens (skip the browser flow on every profile but the seed)
  tok="$dir/mcp-tokens"
  if [ "$slug" = "$SEED_FROM" ]; then
    echo "    = seed profile — OAuth tokens authoritative here"
  elif [ -f "$tok/tinyfish.json" ]; then
    echo "    = OAuth tokens already present"
  elif [ -f "$SEED_DIR/tinyfish.json" ]; then
    mkdir -p "$tok"; cp -R "$SEED_DIR/." "$tok/"
    echo "    + OAuth tokens seeded from '$SEED_FROM'"
  else
    echo "    ! no OAuth tokens — run: hermes -p $slug mcp add tinyfish --url https://agent.tinyfish.ai/mcp"
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
