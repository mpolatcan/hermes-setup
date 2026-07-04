# Web Search — TinyFish & SearXNG

[← All docs](../README.md)

---

## 10. Web search and fetch: TinyFish

Hermes ships with five built-in web backends (Firecrawl, SearXNG, Parallel, Tavily, Exa). TinyFish isn't one of them, but it integrates cleanly via MCP — and for an agent setup, it's a stronger fit than several of the built-in options. This section covers what TinyFish is, why it earns the slot for our nine agents, and how to wire it in.

### 10.1 What TinyFish is and what changes for us

TinyFish is purpose-built for agent web access. Four endpoints under one API key:

- **Search** — `api.search.tinyfish.ai`. Structured JSON results, custom Chromium engine, ~488ms P50 latency, rank-stable across calls. **Free.**
- **Fetch** — `api.fetch.tinyfish.ai`. Renders pages in a real browser, strips nav/scripts/cookie banners, returns clean Markdown/HTML/JSON. **Free.** Failed URLs don't count against quota.
- **Browser** — raw CDP sessions for interactive automation. Metered.
- **Agent** — natural-language browser automation ("go to X, extract Y as JSON"). Metered.

For our nine agents, Search and Fetch are the relevant pair, and they're both free. Free tier is **5 search queries/minute** and **25 fetches/minute** per API key — generous for personal use, but worth knowing per-key.

Why this matters for us specifically:

- **Cleaner context, fewer tokens.** TinyFish Fetch strips boilerplate before returning content. With nine agents talking to an LLM provider, every kilobyte of cookie-banner HTML stripped is real money saved on context tokens.
- **Built for agent patterns, not human eyes.** Search results are structured JSON — title, URL, snippet, position. No HTML parsing required. Rank-stable means repeated calls return the same results in the same order, which makes session reproducibility and `session_search` indexing behave predictably.
- **Live pages, not cached.** Several search backends return stale results. For `researcher` agent's job in particular, this is a meaningful quality difference.
- **One key handles all nine agents.** Sign up once at agent.tinyfish.ai, distribute the key to whichever agents need search. No per-agent provider setup.

Trade-offs to know:

- **Free tier is rate-limited.** 5 searches/minute across one API key. If `researcher` and `coder` both decide to do a deep dive simultaneously, they'll hit the cap. We mitigate by either using separate keys for high-traffic agents or queuing through one shared key.
- **Not built-in to Hermes.** We wire it in via MCP (recommended) or via the OpenAI-compatible custom-backend pattern. Both work; MCP is cleaner.
- **External dependency.** If TinyFish goes down or your account is rate-limited, agents lose web access. We configure a fallback to a built-in Hermes backend (SearXNG self-hosted or Tavily as a paid alternative) so the agents degrade gracefully.

### 10.2 Integration via MCP (recommended)

TinyFish exposes an MCP server at `https://agent.tinyfish.ai/mcp`. Hermes has first-class MCP support, so this is a one-line addition per agent.

#### Step 1: Get an API key

Sign up at `agent.tinyfish.ai`. No credit card required for the free tier. Copy the key.

#### Step 2: Decide on key strategy

Two reasonable patterns:

**Single shared key.** One TinyFish account, key in every agent's config. Simple to manage; everyone shares the 5/min rate limit. Good starting point.

**Per-agent keys.** Separate TinyFish account per high-traffic agent. Eliminates the cross-agent rate limit; more setup overhead. Worth doing only if Single shared key hits the rate limit in practice.

Start with single shared key. Upgrade only if you observe rate-limit errors in agent logs.

#### Step 3: Add the TinyFish MCP server to each agent

> **Verified on the Mini (v0.16.0):** TinyFish's MCP endpoint authenticates with **OAuth 2.1 PKCE**, *not* an `X-API-Key` header. You do **not** hand-write the MCP block and you do **not** need `TINYFISH_API_KEY` for the MCP (the key only matters if you also call TinyFish's REST API directly; the MCP path ignores it). Use the CLI — it runs the OAuth flow and writes the config for you:

```bash
hermes -p researcher mcp add tinyfish --url https://agent.tinyfish.ai/mcp
#   → "Does this server require authentication? [Y/n]"  → Y
#   → opens a browser for the TinyFish OAuth consent (once per profile)
```

This writes the real schema to `~/.hermes/profiles/<slug>/config.yaml`:

```yaml
mcp_servers:                       # flat key — NOT `mcp:` → `servers:`
  tinyfish:
    url: https://agent.tinyfish.ai/mcp
    auth: oauth
    enabled: true
```

…and stores the OAuth token triplet under `~/.hermes/profiles/<slug>/mcp-tokens/` (`tinyfish.json`, `tinyfish.client.json`, `tinyfish.meta.json`).

#### Step 4: Authenticate each profile individually — do NOT copy tokens

> **This is the one thing that bites.** OAuth tokens are **not shareable across profiles.** TinyFish's MCP endpoint uses **per-client Dynamic Client Registration + refresh-token rotation**: each profile that runs `mcp add` gets its *own* `client_id` and its *own* token family. If you copy one profile's triplet into the others, they all share a single `client_id` and a single refresh token — and OAuth 2.1 rotates refresh tokens on every use. The first gateway to refresh gets a new token; the others are left holding the now-revoked one, and the server's **reuse-detection then invalidates the entire token family** (a replayed refresh token reads as theft). A copied triplet *appears* to work for ~1 hour — the access token is still valid — then every profile but one starts failing `mcp test`. That window is exactly why this looked fine on first check and broke later.

So authenticate every profile on its own. Each opens a browser consent once (single-user box — same TinyFish account, but a distinct OAuth client per profile, which is what the protocol wants):

```bash
for p in general researcher assistant marketing coder writer producer finance health; do
  hermes -p "$p" mcp add tinyfish --url https://agent.tinyfish.ai/mcp   # → Y, browser consent
done
```

If a profile is currently broken because a token was copied into it, wipe the bad triplet first so it re-auths clean:

```bash
rm -rf ~/.hermes/profiles/<slug>/mcp-tokens
hermes -p <slug> mcp add tinyfish --url https://agent.tinyfish.ai/mcp
```

`scripts/wire-tinyfish.sh` automates the rest: it writes the `mcp_servers` block + the SearXNG `web:` fallback, strips `web` from `disabled_toolsets`, **launches the per-profile OAuth flow for any profile missing a token** (`AUTH=1`, the default), and restarts every gateway. To repair the copied-token profiles, run it with `FRESH=1` over just those slugs:

```bash
FRESH=1 SLUGS="general researcher assistant coder writer producer" ./scripts/wire-tinyfish.sh
```

#### Step 5: Restart and verify

```bash
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway-researcher
hermes -p researcher mcp test tinyfish      # → ✓ Connected, Tools discovered: 17
```

`mcp test` is the real check (not log-grepping). On success it lists the 17 TinyFish tools — the relevant ones are **`search`** and **`fetch_content`** (free, token-efficient); the rest are metered browser-automation tools. Note the actual tool names are `search` / `fetch_content`, not `tinyfish_search` / `tinyfish_fetch`.

### 10.3 Tell each agent when to reach for TinyFish

MCP gives the agent access to the tools, but the agent still has to *choose* to call them over its built-in `web_search` and `web_extract`. Without guidance, Hermes will reach for whatever its default `web.backend` says. We want TinyFish to win for most calls.

Two layers of guidance.

**Layer 1 — Disable the built-in `web_search` in agents that should use TinyFish exclusively.** In each agent's `config.yaml`:

```yaml
agent:
  disabled_toolsets:
    - web    # disables built-in web_search / web_extract / web_crawl
```

This forces the agent to use the MCP-provided TinyFish tools. Cleanest approach. The downside: no automatic fallback if TinyFish is down.

**Layer 2 — Keep both enabled, guide via SOUL.md.** Leave the `web` toolset enabled and add to each agent's SOUL.md:

```
For web search and page fetches, prefer the TinyFish MCP tools
(`search`, `fetch_content`). They return cleaner, lower-noise
results. Use the built-in web tools only if TinyFish is unavailable
or returns no useful results.
```

The agent will reach for TinyFish first and fall back to built-in tools if needed. Less deterministic but more resilient.

**Recommendation for our setup: every agent is Layer 2** — TinyFish primary, SearXNG fallback. There is no TinyFish-only tier anymore. The whole fleet should *prefer* TinyFish (cleaner, structured, live) and *degrade* to self-hosted SearXNG when TinyFish is rate-limited or down — search should never hard-fail on any agent, including the ones whose web use is only occasional (`coder`, `writer`, `producer`). Cost of uniformity: the built-in `web` toolset's schemas now sit in those three agents' prompts too (the reason they were Layer 1) — accepted in exchange for a fallback that always fires. `producer` is the one judgment call: it's mostly offline rubric scoring, but it still gets the same stack so a candidate check never dead-ends.

### 10.4 Fallback: SearXNG on the Mini

For agents using Layer 2 (TinyFish preferred, fallback enabled), the built-in fallback should be SearXNG running locally — not Firecrawl or Tavily, both of which are paid external APIs that recreate the dependency we're trying to mitigate.

SearXNG is free, self-hosted, privacy-respecting, queries 70+ search engines. It runs as one more container on the Mini.

Add to the Mini's compose file:

```yaml
  searxng:
    image: searxng/searxng:latest
    container_name: searxng
    restart: unless-stopped
    ports:
      - "127.0.0.1:8888:8080"
    volumes:
      - ~/.searxng:/etc/searxng:rw
    environment:
      - BASE_URL=http://127.0.0.1:8888/
      - INSTANCE_NAME=hermes-search-fallback
    deploy:
      resources:
        limits:
          memory: 512M
          cpus: "0.5"
```

Initialize the SearXNG config directory before first run:

```bash
mkdir -p ~/.searxng
# SearXNG auto-generates a default settings.yml on first start
# After it starts, edit ~/.searxng/settings.yml to enable the json format:
#   search:
#     formats: [html, json]
```

Then in each Layer-2 agent's `config.yaml`, add SearXNG as the fallback web backend:

```yaml
web:
  backend: searxng
  search_backend: searxng
  # extract_backend left unset — agent will use TinyFish via MCP for fetch
```

And the env var — **on all nine**, since every agent is Layer 2:

```bash
for p in general researcher assistant marketing coder writer producer finance health; do
  echo "SEARXNG_URL=http://127.0.0.1:8888" >> ~/.hermes/profiles/$p/.env
done
```

**The fallback only fires if the built-in `web` toolset is enabled.** `coder`, `writer`, and `producer` previously disabled `web` (TinyFish-only) — remove it from their `disabled_toolsets` ([docs/05 §6.6](05-deployment.md)) or SearXNG can never kick in. `scripts/wire-tinyfish.sh` does all of this — keys, the `web: backend: searxng` block, *and* stripping `web` from `disabled_toolsets` — across all nine, then restarts the gateways. The snippets here are what it automates.

Now every agent has TinyFish as primary (via MCP), SearXNG as fallback (via built-in `web_search`). If TinyFish rate-limits or goes down, the agent still has working search.

Resource impact: SearXNG adds ~500 MB to the Mini. With Honcho (~1.5 GB) and SearXNG (~500 MB) in Docker plus the native agents, the Mini sits at ~11–13 GB out of 16 GB. Tight but still within budget.

### 10.5 Verifying TinyFish is actually being used

Two checks to run after first setup, and periodically afterward.

**Check 1: Tool usage in agent logs.**

```bash
tail -n 200 ~/.hermes/profiles/researcher/logs/gateway.log | grep -iE "tool_use|tinyfish|search|web_search"
```

You want to see TinyFish's `search` / `fetch_content` calls dominating, with the built-in `web_search` (SearXNG fallback) only firing rarely or never.

**Check 2: TinyFish dashboard.**

Log into `agent.tinyfish.ai`. The dashboard shows queries per day and which key issued them. You should see steady usage from your agents. If usage is zero despite agents doing web work, the MCP wiring isn't picking up — go back to Step 5 of Section 10.2.

**Rate-limit watch.**

If you see `429` errors from TinyFish in logs, the free tier's 5/min cap is being hit. Options:

1. Add a second TinyFish key for high-traffic agents (typically `researcher`).
2. Upgrade to a paid tier ($13/month for 1,650 credits if you also need the metered Agent/Browser endpoints; the free Search/Fetch limits also increase).
3. Stagger heavy work via cron (probably not worth it for hobby use).

### 10.6 Per-agent web configuration summary

For reference when wiring up each agent:

| Agent | TinyFish MCP | Built-in `web` toolset | Fallback | Notes |
|---|---|---|---|---|
| `general` (Derya) | Yes (light use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — conversational main line |
| `researcher` (Doruk) | Yes (heavy use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — needs resilience |
| `assistant` (Tuna) | Yes (light use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — for occasional lookups |
| `marketing` (Nilay) | Yes (heavy use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — competitor/ASO/trend research |
| `coder` (Naz) | Yes (medium use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — docs/Stack Overflow lookups |
| `writer` (Ozan) | Yes (light use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — research while drafting |
| `producer` (Sarp) | Yes (light use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — offline rubric scoring, but web never dead-ends |
| `finance` (Murat) | Yes (heavy use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — markets/news/Reddit research (docs/02 §2.2) |
| `health` (Defne) | Yes (medium use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — nutrition/training research (docs/02 §2.2) |

**Every agent is Layer 2** — TinyFish primary via MCP, SearXNG fallback via the built-in `web` toolset. None is walled off from search and none can hard-fail on it. The earlier TinyFish-only tier (`coder`/`writer`) is retired: they now keep `web` enabled (with `backend: searxng`) so an outage degrades instead of breaking.

### 10.6a URL extraction — TinyFish carries it; no built-in extract backend

Search and extraction are separate capabilities in Hermes. SearXNG is **search-only**: the built-in `web_extract` tool needs an extract-capable backend — v0.16.0's provider registry supports `firecrawl`, `tavily`, `exa`, and `parallel` (`searxng`/`ddgs`/`brave-free` return a typed "search-only" error, no silent fallback).

**Current config: no `extract_backend` is set.** Page extraction runs through **TinyFish MCP** (its fetch/agent endpoints), which every profile already has — consistent with TinyFish-primary above. `web_extract` itself returns a clean "no extract provider configured" error, which is fine: the agents route around it.

If a built-in extract backend is ever wanted, in order of preference:

1. **Tavily** — free tier (~1,000 credits/mo), one `TAVILY_API_KEY`, zero containers.
2. **Nous managed tool gateway** — Hermes can proxy Firecrawl through a Nous subscription token (no API key, no self-host). Requires a Nous sub (none held today).
3. **Firecrawl cloud** — `extract_backend: firecrawl` + `FIRECRAWL_API_KEY`.
4. **Self-hosted Firecrawl — only with a hard memory cap.** This was tried: a 5-container stack (`~/firecrawl`, compose dir kept) wired as `extract_backend: firecrawl` on all 9 profiles. `firecrawl-api` leaks (2+ GiB within minutes) and its compose `mem_limit: 8G` exceeded the whole VM, so the kernel OOM killer shot Honcho/postgres at random. Removed 2026-07-04; the VM is now capped at 4 GB and holds only Honcho + SearXNG. If reviving: `mem_limit: 2G` on `api`, `1G` on `playwright-service`, or it will repeat.

### 10.7 Per-agent skill for TinyFish (optional but recommended)

TinyFish ships an "Agent Skill" — a `SKILL.md` file that teaches the agent when to reach for search vs. fetch vs. agent vs. browser. For Hermes, the skills system is the natural home for this.

```bash
# In each agent that uses TinyFish, install the skill
# (Run from inside the agent's CLI or via skill_manage)
hermes -p researcher skills install use-tinyfish
```

If you'd rather install it manually, the equivalent is to drop the skill into the profile's skills directory:

```bash
mkdir -p ~/.hermes/profiles/researcher/skills/use-tinyfish
# Get the SKILL.md from https://github.com/tinyfish-io/tinyfish-cookbook
# Copy it into the skill directory
```

The skill teaches the agent the escalation ladder: search for finding URLs → fetch for reading pages → agent for interactive tasks → browser for raw CDP. With the skill installed, the agent gets clearer guidance on which TinyFish endpoint to use when — better than relying on SOUL.md hints alone.

### 10.8 What to revisit later

- **Paid tier?** Free is enough for personal multi-agent use. Revisit only if rate limits become a recurring friction.
- **TinyFish Agent and Browser endpoints?** Metered, so they cost credits per call. Worth experimenting with for `researcher` if a workflow needs login-walled pages or multi-step browsing. Start with the free Search + Fetch.
- **Replacing built-in fallback entirely?** Once you've run TinyFish-primary for a month and confirmed reliability, you can drop SearXNG to save the 500 MB. Defensive setup first, optimization later.

---
