# Web Search — TinyFish & SearXNG

[← All docs](../README.md)

---

## 10. Web search and fetch: TinyFish

Hermes ships with five built-in web backends (Firecrawl, SearXNG, Parallel, Tavily, Exa). TinyFish isn't one of them, but it integrates cleanly via MCP — and for an agent setup, it's a stronger fit than several of the built-in options. This section covers what TinyFish is, why it earns the slot for our seven agents, and how to wire it in.

### 10.1 What TinyFish is and what changes for us

TinyFish is purpose-built for agent web access. Four endpoints under one API key:

- **Search** — `api.search.tinyfish.ai`. Structured JSON results, custom Chromium engine, ~488ms P50 latency, rank-stable across calls. **Free.**
- **Fetch** — `api.fetch.tinyfish.ai`. Renders pages in a real browser, strips nav/scripts/cookie banners, returns clean Markdown/HTML/JSON. **Free.** Failed URLs don't count against quota.
- **Browser** — raw CDP sessions for interactive automation. Metered.
- **Agent** — natural-language browser automation ("go to X, extract Y as JSON"). Metered.

For our seven agents, Search and Fetch are the relevant pair, and they're both free. Free tier is **5 search queries/minute** and **25 fetches/minute** per API key — generous for personal use, but worth knowing per-key.

Why this matters for us specifically:

- **Cleaner context, fewer tokens.** TinyFish Fetch strips boilerplate before returning content. With seven agents talking to an LLM provider, every kilobyte of cookie-banner HTML stripped is real money saved on context tokens.
- **Built for agent patterns, not human eyes.** Search results are structured JSON — title, URL, snippet, position. No HTML parsing required. Rank-stable means repeated calls return the same results in the same order, which makes session reproducibility and `session_search` indexing behave predictably.
- **Live pages, not cached.** Several search backends return stale results. For `researcher` agent's job in particular, this is a meaningful quality difference.
- **One key handles all seven agents.** Sign up once at agent.tinyfish.ai, distribute the key to whichever agents need search. No per-agent provider setup.

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

#### Step 3: Store the key in each agent's `.env`

Edit each agent's env file. On the Mini:

```bash
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes/profiles/researcher/.env
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes/profiles/assistant/.env
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes/profiles/coder/.env
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes/profiles/writer/.env
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes/profiles/marketing/.env   # market/competitor research
```

#### Step 4: Configure the MCP server in each agent's `config.yaml`

The agent reads its config from `~/.hermes/profiles/<name>/config.yaml`. Add:

```yaml
mcp:
  servers:
    tinyfish:
      url: "https://agent.tinyfish.ai/mcp"
      headers:
        X-API-Key: "${TINYFISH_API_KEY}"
      enabled: true
```

Hermes's environment substitution syntax (`${TINYFISH_API_KEY}`) reads from the `.env` file in the profile's data dir (`~/.hermes/profiles/<name>/.env`), which is the file we wrote in Step 3.

#### Step 5: Restart the agent and verify

```bash
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway-researcher
tail -n 50 ~/.hermes/profiles/researcher/logs/gateway.log | grep -i "mcp\|tinyfish"
```

You should see TinyFish MCP server registered and the agent picking up `tinyfish_search` and `tinyfish_fetch` tools. From a chat with the agent, ask "what tools do you have for web search?" — `tinyfish_search` and `tinyfish_fetch` should appear in the list.

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
(tinyfish_search, tinyfish_fetch). They return cleaner, lower-noise
results. Use the built-in web tools only if TinyFish is unavailable
or returns no useful results.
```

The agent will reach for TinyFish first and fall back to built-in tools if needed. Less deterministic but more resilient.

**Recommendation for our setup:**

- `researcher`, `assistant`, `marketing`: **Layer 2** (prefer TinyFish, fall back to SearXNG). These agents need web access to function — `marketing` for competitor/ASO/trend research; outages should degrade, not break.
- `coder`, `writer`: **Layer 1** (TinyFish only). Their web use is opportunistic; if TinyFish is down, asking the user to try again is fine.

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

And the env var:

```bash
echo "SEARXNG_URL=http://127.0.0.1:8888" >> ~/.hermes/profiles/researcher/.env
echo "SEARXNG_URL=http://127.0.0.1:8888" >> ~/.hermes/profiles/assistant/.env
```

Now the agent has TinyFish as primary (via MCP), SearXNG as fallback (via built-in `web_search`). If TinyFish rate-limits or goes down, the agent still has working search.

Resource impact: SearXNG adds ~500 MB to the Mini. With Honcho (~1.5 GB) and SearXNG (~500 MB) in Docker plus the native agents, the Mini sits at ~11–13 GB out of 16 GB. Tight but still within budget.

### 10.5 Verifying TinyFish is actually being used

Two checks to run after first setup, and periodically afterward.

**Check 1: Tool usage in agent logs.**

```bash
tail -n 200 ~/.hermes/profiles/researcher/logs/gateway.log | grep -i "tool_use\|tinyfish_search\|web_search"
```

You want to see `tinyfish_search` and `tinyfish_fetch` calls dominating, with `web_search` (the built-in fallback) only firing rarely or never.

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
| `coder` (Naz) | Yes (medium use) | Disabled | None | Layer 1 — docs/Stack Overflow lookups |
| `writer` (Ozan) | Yes (light use) | Disabled | None | Layer 1 — research while drafting |
| `producer` (Sarp) | No | Disabled | None | Offline rubric scoring |

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
