# Model Providers

[← All docs](../README.md)

---

```mermaid
flowchart LR
    Naz & Ozan --> codex["Codex · gpt-5.x<br/>ChatGPT sub · accepted-risk"]
    Derya & Doruk & Tuna & Sarp & Nilay --> mini["MiniMax · M3 standard<br/>$20 token plan · ~4.5k req/5h"]
    aux["all agents · aux tasks<br/>vision / summarize / compress"] --> or["OpenRouter · Gemini Flash<br/>API key · pay-per-token · overflow valve"]
    codex -. fallback .-> mini
    mini -. fallback/overflow .-> or
    classDef codex fill:#FB8C00,stroke:#E65100,color:#fff
    classDef mini fill:#43A047,stroke:#1B5E20,color:#fff
    classDef ext fill:#00897B,stroke:#004D40,color:#fff
    classDef neutral fill:#546E7A,stroke:#263238,color:#fff
    class Naz,Ozan,codex codex
    class Derya,Doruk,Tuna,Sarp,Nilay,mini mini
    class or ext
    class aux neutral
```

## 5. Model providers per agent

The subscriptions actually on hand: **MiniMax**, **Codex** (ChatGPT), **Claude Code**, and **Gemini Antigravity**. Not all of them are *safe* to wire into a third-party agent like Hermes — "works technically" and "won't get your account flagged" are different questions. This doc maps them to a safe, sustainable stack:

- **MiniMax = primary** (5 of 7 agents). Zero risk.
- **Codex = accepted-risk extra** on the two **quality-critical** agents (`writer`, `coder`) — gpt-5.x for prose + code. ⚠️ `coder` is high-volume; see the risk note in 5.0.
- **OpenRouter API key** for cheap aux + cross-provider fallback. Safe.
- **Claude** via a plain Anthropic API key *only if you want it* — not the Claude Code subscription.
- **Gemini Antigravity = not usable.** Read 5.0.

### 5.0 Provider safety & ToS — read first

The distinction that governs everything below: **an API key (you pay per token) is always safe. A consumer *subscription* OAuth token piped into a third-party agent is a gray area — and in one case, a banned one.**

| Subscription | Hermes provider | Works? | Safe? |
|---|---|---|---|
| **MiniMax** | `minimax` (API key) / `minimax-oauth` | ✅ | ✅ **Safe** — built for programmatic use |
| **Codex** (ChatGPT) | `openai-codex` (OAuth) | ✅ | ⚠️ **Against consumer ToS** — sub-OAuth in a non-OpenAI app; *unenforced today*, but Anthropic + Google killed the equivalent Apr 2026 |
| **Claude Code** | `anthropic` (OAuth, reads Claude Code's cred store) | ✅ | ⚠️ **Gray + high-stakes** — risks the sub you code with |
| **Gemini Antigravity** | — (no API) | ❌ | 🚫 **Banned pattern** — use an AI Studio API key instead |

- **MiniMax** — API key or its own browser OAuth. Designed for this. Make it your primary.
- **Codex** — works via device-code OAuth, but it reuses your ChatGPT subscription **outside OpenAI's own clients.** Be clear-eyed: this is **against OpenAI's consumer terms** — they prohibit automated/programmatic access and "using ChatGPT to power third-party services," and OpenAI has **declined to bless** sub-OAuth in third-party apps (the feature ships in official Codex tooling only). It is *not currently enforced* at personal scale — which is exactly why the OpenClaw/OpenCode community moved **to** Codex after **Anthropic and Google clamped down on theirs in April 2026.** That also makes OpenAI the **likely next** to follow. No-warning account bans of Codex+sub users are documented though rare. **So: accepted-risk, not permitted.** Run it on the two **quality-critical** agents — `writer` (occasional, low-volume) and `coder` (higher-volume, but **interactive/human-paced** — you drive it on-demand, *not* a 24/7 cron, and automated cadence is what the flag targets). Keep **no automated cron on Codex** (the weekly scout lives on MiniMax), isolate it, and keep the API-key escape hatch ready (5.3). **`coder`'s volume makes it the one to watch** — A/B it against M3 first, since M3 is itself a strong coder. The tail risk is your ChatGPT account.
- **Claude Code subscription** — Hermes can read Claude Code's credential store (`anthropic` OAuth), but that points your *coding* subscription at a different agent. If flagged, you risk the tool you actually develop with. **Keep Claude Code for Claude Code.** Want Claude inside Hermes? Use a separate **Anthropic API key** (pay-per-token, unambiguously fine) — see 5.4.
- **Gemini Antigravity** — **do not attempt.** Antigravity is an IDE with no API to extract auth from. The closest pattern, `google-gemini-cli` OAuth, is exactly what Google **enforced against in early 2026** — paid subscribers using Gemini-CLI-style OAuth in third-party apps lost access during the crackdown. The only safe way to use Gemini in Hermes is an **AI Studio API key** (free tier or pay-per-token) or **Gemini via OpenRouter** — both separate from your Antigravity subscription. The OpenRouter→Gemini-Flash aux route in 5.5 is safe precisely because it's an API key, not subscription OAuth.

**The rule:** never put gray-area subscription-OAuth on an **always-on, automated** agent that hammers it — automated cadence is the enforcement trigger (that's what the Google/Anthropic clampdowns targeted). So the only **cron** agent (`research`'s weekly scout) runs on **MiniMax**, and Codex carries only the two **human-paced** quality agents (`writer` occasional; `coder` interactive/on-demand — you're at the keyboard). Everything else rides the **MiniMax $20 Token Plan** (a flat sub, ~4,500-request / 5-hour rolling quota — see 5.2). **OpenRouter (true pay-per-token) is the overflow valve.** Note the mental-model shift: MiniMax here is *not* pay-per-token — it's a capped sub like ChatGPT, just a safe one. The residual exposure is `coder`'s volume on Codex — accepted for gpt-5.x coding quality, watched via the shared-quota note (5.3) and the M3 A/B.

### 5.1 Per-agent model assignment

| Slug | Bot | Main model | Provider | Safety | Why |
|---|---|---|---|---|---|
| `general` | Derya | `MiniMax-M3` | `minimax` | ✅ | Highest-volume daily driver → $20 token plan; 5h quota is generous for solo use |
| `research` | Doruk | `MiniMax-M3` | `minimax` | ✅ | Web-tool-driven; M3's browsing + 1M context fit it, and it keeps the weekly cron off the gray-area sub |
| `concierge` | Tuna | `MiniMax-M3` | `minimax` | ✅ | Daily logistics |
| `ops` | Nilay | `MiniMax-M3` | `minimax` | ✅ | Light, deterministic; standard M3 on the entry token-plan tier |
| `coder` | Naz | `gpt-5.x` | `openai-codex` | ⚠️ | gpt-5.x coding quality; interactive/human-paced (lower flag risk than a cron). **Heaviest agent → watch the ChatGPT quota**; MiniMax fallback |
| `writer` | Ozan | `gpt-5.x` | `openai-codex` | ⚠️ | Voice, long-form; *occasional* |
| `producer` | Sarp | `MiniMax-M3` | `minimax` | ✅ | Idea scoring (Phase B) |

For all seven, the **auxiliary model** (vision, web summarization, context compression, session search) is `google/gemini-2.5-flash` via OpenRouter (5.5) — short, frequent calls routed to the cheapest fast model.

Why Codex lands on `coder` + `writer`: those are the two **quality-critical creative** agents — gpt-5.x is strong at code and long-form prose, and that's where you most want the frontier model. The safety guard is that **neither runs as an automated cron**: `writer` is occasional, `coder` is interactive (you drive it at the keyboard). The one cron — `research`'s weekly scout — lives on **MiniMax**, so no *automated* volume ever hits Codex. The honest caveat: `coder` is the **heaviest** agent, so it's the real ToS/quota exposure here — watch the shared ChatGPT quota (it's shared with `writer`), and **A/B `coder` against MiniMax-M3** (a strong agentic coder itself, SWE-Bench 59 / Terminal-Bench 66). If gpt-5.x isn't clearly better for GDScript, move `coder` back to M3 (`/model` per session, or the config) — safer and $0.

### 5.2 MiniMax setup — your primary (Derya, research, concierge, ops, producer)

Get an API key at platform.minimax.io. Two regional endpoints:

- **Global** (`minimax`, `api.minimax.io`) — use this unless you're in mainland China.
- **China** (`minimax-cn`, `api.minimaxi.com`) — for China-region accounts.

One Token-Plan key works across every MiniMax agent — all five share the plan's rolling request quota:

```bash
for agent in general research concierge ops producer; do
  echo "MINIMAX_API_KEY=mn_..." >> ~/.hermes/${agent}/.env
done
```

`config.yaml` — **all** MiniMax agents use standard `MiniMax-M3`:

```yaml
model:
  provider: minimax
  default: MiniMax-M3
```

**You're on the entry $20 Token Plan, running `MiniMax-M3`** (the June 2026 flagship — 1M context, multimodal, stronger agentic/coding than M2.7). It's a **flat monthly sub with a rolling request quota** — roughly **4,500 requests per moving 5-hour window**, auto-recovering — *not* pay-per-token. Cost is fixed at $20 regardless of volume; the ceiling is the **request quota** (and the entry tier's speed), not dollars. **All five MiniMax agents share that one quota.** Higher token-plan tiers (~$50 / ~$120) buy more quota/throughput if you outgrow it. For a solo user the 5-hour quota is generous; if it bites mid heavy-`coder` session, the OpenRouter fallback (5.7) is the overflow valve. The big M3 win for this fleet: the 1M window helps `research` synthesize long sources and `coder` hold a whole project in context. *(Config note: same `provider: minimax` wiring — only billing differs from pay-per-token. Verify at platform.minimax.io that **M3 is included at your $20 tier** and the exact model string + base URL; token plans can use a distinct endpoint, and >512K-token requests carry a 2× long-context multiplier.)*

**MiniMax OAuth alternative.** `minimax-oauth` logs in via browser (free tier, no API billing). It's also *relatively* safe — it's MiniMax's own product — but the free tier rate-caps harder and adds per-profile OAuth bootstrap. For always-on agents an API key is more reliable. Flip with `provider: minimax-oauth` if cost ever bites.

### 5.3 Codex OAuth setup — accepted-risk (coder + writer only)

> ⚠️ **Against OpenAI's consumer ToS, accepted (5.0).** Keep it to these two agents, **human-paced** (no automated cron on Codex). `writer` is occasional; `coder` is interactive (you drive it) but **high-volume** — it's the real exposure here. Decision on record: we run it for gpt-5.x's code + prose quality, knowing it's unenforced-today, *not* permitted. If you'd rather carry zero ToS risk, put `coder`/`writer` on an API key (escape hatch below) or MiniMax-M3 and skip the OAuth.

Codex uses OAuth against your ChatGPT account — no API billing, your subscription pays. The token saves to `~/.hermes/<slug>/auth.json` per profile and survives restarts (persisted under `~/.hermes/<slug>/`). Two ways to bootstrap.

**Path A — import existing Codex credentials** (if you use ChatGPT Desktop or the Codex CLI). Your creds live at `~/.codex/auth.json`; Hermes auto-imports on first start:

```bash
# after: hermes profile create coder
cp ~/.codex/auth.json ~/.hermes/coder/auth.json
chmod 600 ~/.hermes/coder/auth.json
# repeat for ~/.hermes/writer
```

**Path B — fresh device-code login** (if you don't have Codex creds yet):

```bash
hermes setup --profile coder
# at the model step pick "OpenAI Codex"; open the printed URL, paste the code, approve
```

Either way, `config.yaml`:

```yaml
model:
  provider: openai-codex
  default: gpt-5.3
```

**Shared quota — watch this.** Both Codex agents draw on the **same ChatGPT account cap** (Plus vs Pro differ). `writer` is light, but **`coder` is your heaviest agent** — a long coding day can strain the shared cap and starve `writer`, or hit the ChatGPT limit. If that bites, fall `coder` back to MiniMax-M3 (it's a strong coder; `/model MiniMax-M3` per session or the config). `invalid_grant` in logs = the refresh token was revoked (password change / remote signout); redo Path A or B.

**Escape hatch — keep an OpenAI API key ready.** Codex-via-sub is the *last* of the big-three sub-OAuth paths still working (Anthropic + Google enforced theirs in April 2026); OpenAI is the likely next. Treat it as **temporary.** The clean swap is **one line per agent** — drop the OAuth, point at an OpenAI API key:

```yaml
# ~/.hermes/coder/config.yaml — the day Codex-OAuth stops or the risk isn't worth it
model:
  provider: openai          # API key (platform.openai.com), NOT openai-codex OAuth
  default: gpt-5.4
```

Add `OPENAI_API_KEY=sk-...` to that agent's `.env`. Same GPT-5.x models, pay-per-token, **zero ToS risk**. `writer` is cheap (occasional); `coder` is heavier, so an API key there costs real per-token money on active dev days — which is the trade-off vs the $0 sub. Keep a key on hand so a clampdown is a config flip, not an outage. (Cheaper clean swaps: **MiniMax-M3** for `coder` — $0 on your plan, strong coder — or **Gemini 3.1 Pro** for `writer`.)

### 5.4 Anthropic — optional, API key only (never the Claude Code sub)

Not in the default stack. Add it only if you specifically want Claude for an agent (most likely `coder` on a hard refactor day). **Use an API key, not your Claude Code subscription** (5.0).

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> ~/.hermes/coder/.env
```

```yaml
model:
  provider: anthropic
  default: claude-sonnet-4-6
```

Pay-per-token via console.anthropic.com. Switch to Opus for hard work with `/model claude-opus-4-6` in-session (same provider). **Do not** use `hermes model → Anthropic OAuth` with your Claude Code/Max login — that's the gray-area path that risks your coding subscription.

### 5.5 OpenRouter for auxiliary tasks (one key, all seven agents)

Aux tasks fire constantly — vision, page summaries, session titles, context compression. Hermes defaults them to the main chat model unless overridden. Routing them to a cheap fast model via OpenRouter saves real money, and — importantly — it's an **API key**, so this is the *safe* way to use Gemini (not the banned subscription-OAuth route from 5.0).

Get a key at openrouter.ai → Keys, add $5–10 of credit (lasts months). Add it to **every** agent's `.env`:

```bash
for agent in general research concierge ops coder writer producer; do
  echo "OPENROUTER_API_KEY=sk-or-..." >> ~/.hermes/${agent}/.env
done
```

Then in **every** `config.yaml`:

```yaml
auxiliary:
  vision:
    provider: openrouter
    model: google/gemini-2.5-flash
  web_extract:
    provider: openrouter
    model: google/gemini-2.5-flash
  session_search:
    provider: openrouter
    model: google/gemini-2.5-flash
  compression:
    provider: openrouter
    model: google/gemini-2.5-flash
  approval:
    provider: openrouter
    model: google/gemini-2.5-flash
```

Main chat stays on MiniMax/Codex; everything else routes through cheap Gemini Flash. Gemini Flash wins here on cost, sub-second latency, multimodal vision, and generous OpenRouter rate limits.

**Why not put aux on MiniMax to save a provider?** Tempting, but aux fires *constantly* — every vision call, every page summary, every title. On the $20 Token Plan those tiny calls would drain the **shared 5-hour request quota** and starve your real agents, and there's no cheap highspeed tier to absorb them. Keep aux on **OpenRouter pay-per-token**: cheap, fast, and — crucially — it doesn't touch the MiniMax quota. The few dollars of OpenRouter spend buys quota protection.

### 5.6 Cost ballpark (per month, personal use)

Two flat subscriptions plus a little pay-per-token. There is **no per-agent metering** — the five MiniMax agents all run inside one $20 plan, so volume doesn't move the bill.

| Line | Provider | Cost |
|---|---|---|
| MiniMax agents (Derya, Doruk, Tuna, Nilay, Sarp) | **$20 Token Plan** (Plus, standard) | **$20/mo flat** — shared 5h request quota, no per-token billing |
| Codex agents (Naz, Ozan) | ChatGPT sub | **$0 extra** — included; but `coder` (Naz) is heavy → watch the ChatGPT cap |
| Aux (all seven) + overflow/fallback | OpenRouter · Gemini Flash (pay-per-token) | **~$1–5/mo** |
| **Total new spend** | | **~$21–25/mo** on top of the ChatGPT sub you already have |

The real constraint is the **request quota**, not dollars — so the bill won't surprise you. If the shared 5-hour quota gets tight (heavy daily `coder` use), the levers are: route `coder` to the OpenRouter fallback during big sessions (5.7), or step up a MiniMax token-plan tier (~$50 / ~$120 = more quota/throughput). Claude stays opt-in (5.4).

### 5.7 Fallback chains per agent

Hermes retries a failed primary (rate limit, 5xx, auth) against the next provider in the chain without losing the conversation. Each agent falls back to a *different* provider so one outage can't take it down. The OpenRouter key from 5.5 covers all of these — **MiniMax agents fall back via OpenRouter so they never need Codex credentials.**

MiniMax-primary agents (`general`, `research`, `concierge`, `producer`):
```yaml
fallback_providers:
  - provider: openrouter
    model: minimax/minimax-m3
  - provider: openrouter
    model: google/gemini-2.5-flash
```

`ops` (standard M3 — keep it light):
```yaml
fallback_providers:
  - provider: openrouter
    model: google/gemini-2.5-flash
```

Codex-primary agents (`coder`, `writer`) — fall back to MiniMax (needs `MINIMAX_API_KEY` in their `.env` too), then OpenRouter:
```yaml
fallback_providers:
  - provider: minimax
    model: MiniMax-M3
  - provider: openrouter
    model: openai/gpt-5
```

So if a Codex call 503s or rate-limits, the session silently continues on MiniMax. If MiniMax hiccups, agents ride OpenRouter. No single provider is a single point of failure.

### 5.8 Putting it together: per-agent `.env` summary

`TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS` are written by `setup-bots.sh` (see [Telegram Bots](03-telegram-bots.md)); the model keys below you add yourself.

`~/.hermes/general/.env` (Derya):
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
MINIMAX_API_KEY=mn_...
OPENROUTER_API_KEY=sk-or-...
```

`~/.hermes/research/.env` (Doruk):
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
MINIMAX_API_KEY=mn_...        # primary
OPENROUTER_API_KEY=sk-or-...
```

`~/.hermes/concierge/.env` (Tuna), `~/.hermes/ops/.env` (Nilay), `~/.hermes/producer/.env` (Sarp):
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
MINIMAX_API_KEY=mn_...
OPENROUTER_API_KEY=sk-or-...
```

`~/.hermes/coder/.env` (Naz) — **Codex-primary**:
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
MINIMAX_API_KEY=mn_...        # for fallback
OPENROUTER_API_KEY=sk-or-...
# Codex credentials live in auth.json, not .env
# ANTHROPIC_API_KEY=sk-ant-...   # optional, only if you want Claude (5.4)
```

`~/.hermes/writer/.env` (Ozan):
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
MINIMAX_API_KEY=mn_...        # for fallback
OPENROUTER_API_KEY=sk-or-...
# Codex credentials live in auth.json
```

`chmod 600` on every `.env`. Bearer credentials — treat them like passwords. (`setup-bots.sh` already chmods the ones it writes.)

### 5.9 Verification

Sanity-check each provider from the host before starting agents:

```bash
# OpenRouter (covers aux + all fallbacks)
curl -s https://openrouter.ai/api/v1/models \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | jq '.data | length'
# Expect a number > 300

# MiniMax
curl -s https://api.minimax.io/v1/text/chatcompletion_v2 \
  -H "Authorization: Bearer $MINIMAX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"MiniMax-M3","messages":[{"role":"user","content":"hi"}],"max_tokens":10}'
# Expect JSON with "choices"

# Anthropic — only if you opted into 5.4
curl -s https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
# Expect JSON with "content"
```

Codex can't be verified outside Hermes — its auth is bound to Hermes's credential store. It fails loudly on first startup (`invalid_grant` / `auth required` in logs) if broken.

### 5.10 Changing providers later

Each agent's `config.yaml` is the source of truth:

```bash
nano ~/.hermes/general/config.yaml   # change model.provider / model.default
launchctl kickstart -k gui/$(id -u)/com.hermes.general
```

History, memory, and skills are provider-agnostic and survive the switch. Use `/model <name>` inside a session to switch *temporarily* without editing the file; persistent changes need the config edit + restart.

### 5.11 Future — expanding via OpenRouter (Nemotron, DeepSeek V4, …)

OpenRouter is already wired for aux + fallback, so adding more models is a **one-line config change, no new account**. When you want to experiment — NVIDIA Nemotron, DeepSeek V4, Qwen, GLM, etc. — just point an agent or an aux task at the OpenRouter slug:

```yaml
model:
  provider: openrouter
  default: deepseek/deepseek-v4        # example — use the exact slug from openrouter.ai/models
```

Good candidates to try as they mature:
- **DeepSeek V4** — cheap strong reasoning; a possible MiniMax alternative for `concierge`/`producer`.
- **NVIDIA Nemotron** — strong open models; worth testing on `coder` against MiniMax.
- **Qwen / GLM / Kimi** — also first-class on OpenRouter (and Hermes has native `qwen-oauth`, `zai`, `kimi-coding` providers if you prefer direct).

Workflow: try a candidate with `/model openrouter/<slug>` in a live session, judge it on your actual tasks, and only persist to `config.yaml` if it clearly beats the incumbent. **Keep MiniMax as primary until something demonstrably wins** — novelty isn't a reason to switch a working agent. All OpenRouter usage is API-key billing, so it's always on the safe side of 5.0.

---
