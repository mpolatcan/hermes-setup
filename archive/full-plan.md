# Hermes Agent — Seven-Agent Deployment Plan

**Hardware:** Mac M4 Mini (16 GB / 256 GB SSD) + MacBook Pro M2 (16 GB / 256 GB SSD)
**Container runtime:** OrbStack
**Network:** Tailscale (private mesh, no public exposure)
**Approach:** Six independent containerized agents, one container per agent, following Nous Research's official recommendation for Docker deployments.

---

## 1. Architectural decision: one container per agent

**The official default changed — and we deliberately diverge from it.** Since the s6-supervision migration, Hermes' own Docker guide recommends the *opposite* of what older write-ups (including an earlier draft of this plan) claimed: *"the s6 supervision tree treats each profile as a first-class supervised service, so the recommended deployment is one container hosting all profiles."* That single-container, multi-profile path is now the simple default — `hermes profile create <name>` inside one shared container, with s6 supervising each gateway.

We don't take it. The same guide lists exactly when one-container-per-profile is the right call: *"resource isolation, independent image pinning, network segmentation, or compliance."* Our setup hits three of those four, so we run **one container per agent** — each a fully independent container with its own host directory (`~/.hermes-<name>/`), its own bot token, its own personality, and its own lifecycle. The container itself *is* the isolation boundary.

This is a conscious trade-off. We give up the single-container conveniences (s6 auto-restart per profile, a shared interpreter cache, the `hermes profile` UX) in exchange for hard kernel-level boundaries between agents. `docker compose` (Section 7) recovers most of the management simplicity. The reasons the trade is worth it for six agents:

- **True isolation.** Each container has its own filesystem, process table, and resource limits. A crash or runaway session in one agent cannot affect the others.
- **Independent lifecycle.** Restart, upgrade, pause, or roll back each agent on its own. `docker restart hermes-research` leaves the other five untouched.
- **Clean port separation.** Each gateway binds its own host port. No risk of cross-talk between chat platforms or API servers.
- **No concurrent-write risk.** The docs warn that two gateways must never run against the same data directory. Container-per-agent makes this structurally impossible.
- **One directory per agent.** Backups, migrations, and permissions all follow the bind-mounted directory — no shared state to disentangle, no `--profile` flags to remember.
- **Credential isolation.** Hermes profiles are explicitly *not* filesystem sandboxes — a shell in one profile can read another profile's `.env`, i.e. its bot token and API keys. Separate containers close that gap: `coder` (the only agent running arbitrary code, with your projects mounted) cannot reach the other agents' credentials. See Section 13's execution-sandboxing notes.

---

## 2. Agent roster and machine assignment

Seven agents across the two machines, split by workload pattern (always-on vs. interactive). Each has a functional **slug** (its container, directory, and port) and a Turkic **display name** (what shows in Telegram). Slugs stay constant and machine-readable; the Turkic names are personality only. Display names are short for the chat list; full honorific forms live in each bot's profile description (Section 4.10).

### M4 Mini — always-on agents (4)

Stays powered on, hosts the agents that need 24/7 presence.

| Slug | Display | Role | Personality | Resources |
|---|---|---|---|---|
| `general` | **Kam** | Talk about anything — your main line; hands off to specialists | Calm, broad, curious; a touch of dry wit | 2 GB RAM, 1 CPU |
| `research` | **Mergen** | Research any domain (game / history / academic); runs the game-scout cron | Methodical, citation-driven, tracks every source | 3 GB RAM, 1 CPU |
| `concierge` | **Umay** | Daily life: calendar, reminders, morning digest via cron | Warm, brief, action-first | 2 GB RAM, 1 CPU |
| `ops` | **Asena** | Watches the fleet/system, status reports, scheduled checks | Terse, status-bar voice, facts only | 1 GB RAM, 0.5 CPU |

### MacBook Pro M2 — interactive on-demand agents (3)

Started when needed, stopped when not.

| Slug | Display | Role | Personality | Resources |
|---|---|---|---|---|
| `coder` | **Ülgen** | Development pair, Godot-first; the only agent that runs code | Direct, opinionated, code-first, shows diffs | 4 GB RAM, 2 CPU |
| `writer` | **Korkut** | Drafts, editing, brainstorming; writes game PRDs | Playful, exploratory, generative | 2 GB RAM, 1 CPU |
| `producer` | **Kayra** | Game-dev discovery: idea backlog + scoring (**Phase B, deferred**) | Skeptical product lead, anti-hype | 2 GB RAM, 1 CPU |

Full forms (bot profiles): Kam Ata, Mergen Han, Umay Ana, Asena Ana, Bay Ülgen, Dede Korkut, Kayra Han. SOULs in Section 6.7. The `concierge` slug is kept for wiring continuity across Sections 3–10; the agent presents as **Umay**, the daily-life agent.

**Resource math:**
- **M4 Mini:** Kam 2 + research 3 + concierge 2 + ops 1 = **8 GB** to containers, plus Honcho (~2 GB, Phase B) + SearXNG (~0.5 GB) + OrbStack (~1.5 GB) + macOS (~2 GB) ≈ **~14 / 16 GB at full build-out**. Tight but fits. **Phase A (research + Kam only) is loose** — you don't approach the ceiling until Honcho lands. Drop `ops` to 0.5 GB if pressure shows.
- **MacBook Pro:** coder 4 + writer 2 (+ producer 2 in Phase B) — but these are **on-demand**. Run what you're using; don't hold all three plus a heavy IDE at once.

### 2.1 Kam — generalist spec (the new primary agent)

`Kam` is the agent you message most: open conversation, brainstorming, quick answers, and hand-offs to the specialists. Always-on on the Mini so it answers from your phone with the laptop closed.

- **Model:** **MiniMax primary, Codex fallback.** Same logic as `coder` — your highest-volume agent goes on pay-per-token MiniMax so it can't blow the shared ChatGPT daily cap, with gpt-5.x as the quality fallback.
  ```yaml
  # ~/.hermes-general/config.yaml
  model:
    provider: minimax
    default: MiniMax-M2.7
  fallback_providers:
    - provider: openai-codex
      model: gpt-5.3
  ```
- **Toolsets** (extends Section 6.6): keep `web`, `vision`, `tts`, `memory`, `session_search`, `skills`, `clarify`, `cronjob`.
  ```yaml
  agent:
    disabled_toolsets: [terminal, code_execution, browser, image_gen, delegation, messaging, todo]
  ```
- **Memory:** full built-in + **Honcho** — Kam builds the richest user model, since it's where you talk about everything. Its own AI peer.
  ```json
  // ~/.hermes-general/honcho.json  → "aiPeer": "kam", "peerName": "<your-name>", "workspace": "hermes"
  ```
- **Port / container:** `8648` → `hermes-general`, data dir `~/.hermes-general/`. Compose entry (Mini):
  ```yaml
  hermes-general:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-general
    restart: unless-stopped
    command: gateway run
    ports:
      - "${TAILSCALE_IP}:8648:8642"
    volumes:
      - ~/.hermes-general:/opt/data
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: "1.0"
  ```
- **SOUL:** Section 6.7. **No `--shm-size`** (browser disabled, per 6.6).

---

## 3. Directory and naming conventions

Each agent gets a dedicated host directory and a predictable container name.

```
~/.hermes-general/     ← bind-mounts to /opt/data inside hermes-general container   (Kam)
~/.hermes-research/    ← bind-mounts to /opt/data inside hermes-research container  (Mergen)
~/.hermes-concierge/   ← bind-mounts to /opt/data inside hermes-concierge container (Umay)
~/.hermes-ops/         ← bind-mounts to /opt/data inside hermes-ops container       (Asena)
~/.hermes-coder/       ← bind-mounts to /opt/data inside hermes-coder container     (Ülgen)
~/.hermes-writer/      ← bind-mounts to /opt/data inside hermes-writer container    (Korkut)
~/.hermes-producer/    ← bind-mounts to /opt/data inside hermes-producer container  (Kayra, Phase B)
```

Each directory contains the full state for that agent:

| Path | Contents |
|---|---|
| `.env` | API keys and bot tokens |
| `config.yaml` | Hermes configuration |
| `SOUL.md` | Agent personality and instructions |
| `sessions/` | Conversation history |
| `memories/` | Persistent memory store (USER.md, MEMORY.md) |
| `skills/` | Agent-created and bundled skills |
| `cron/` | Scheduled job definitions |
| `logs/` | Runtime logs |

**Port allocation** (each gateway needs a unique host port; container-internal stays at 8642):

- `8642` → research (Mergen)
- `8643` → concierge (Umay)
- `8644` → ops (Asena)
- `8645` → coder (Ülgen)
- `8646` → writer (Korkut)
- `8647` → producer (Kayra, Phase B)
- `8648` → general (Kam)
- `9119–9126` → dashboards if enabled (one per agent)

**Important:** Ports are bound to the Tailscale interface only, never `0.0.0.0`. See Section 8 (Tailscale networking) for the exact bind syntax. This means each port is reachable from your other tailnet devices but invisible to your LAN and the public internet.

---

## 4. Telegram bot prerequisites

Each agent connects to a separate Telegram bot. Seven agents = seven bots. Bots have to exist on Telegram's side *before* you bring up the agents, so this is pre-work for Phase 1 of the deployment plan.

Total time: ~20 minutes for all seven bots. Do it in one sitting. (Bot *creation* is manual; everything after is automated — see 4.12.)

### 4.1 What you're creating

For each agent, you need:

- A Telegram **bot** created via @BotFather (one per agent).
- A Telegram **bot token** — a string like `7123456789:AAH1bGciOiJSUzI1NiIsInR5cCI6Ikp...`. Treat this like a password. Anyone with the token controls the bot.
- Your numeric Telegram **user ID** (not your @username) — used to lock each bot down to only you. Single value, reused for all six bots.
- Optional: a friendly **display name** and **description** for each bot, since you'll see them in your Telegram chat list.

### 4.2 Get your Telegram user ID (one-time, do this first)

Open Telegram, search for `@userinfobot`, send it any message. It replies instantly with your numeric ID (e.g. `123456789`). Save this number — you'll paste it into every agent's config.

Alternative bots if `@userinfobot` is unreachable: `@get_id_bot`, `@RawDataBot`.

**Important:** your @username is not your user ID. The ID is a stable number that never changes even if you rename yourself.

### 4.3 Create six bots via BotFather

Open Telegram, search for `@BotFather`, start a conversation. Then for each of your seven agents, repeat this flow (or do the minimum here — just `/newbot` to get tokens — and let 4.12's script set names, descriptions, and commands in bulk):

```
/newbot
→ Bot's display name: Research Assistant (or whatever you want users to see)
→ Bot's username: research_<yourname>_bot   (must end in "bot" and be unique on Telegram)

BotFather replies with the token. Copy it immediately into a notes app — you'll
need it in step 4.4. Then for the same bot, run these to harden settings:

/mybots → pick the bot → Bot Settings
→ /setprivacy → Disable (lets the bot see all messages in groups if you ever add it to one)
→ /setjoingroups → Disable (you only want one-on-one chat with each agent)
→ /setdescription → "Methodical research assistant" (shows on the bot's profile)
→ /setabouttext → "Hermes Agent — research personality" (shows in bot info)
```

Suggested username pattern: `<role>_<yourname>_bot`. So `research_alice_bot`, `concierge_alice_bot`, `ops_alice_bot`, `coder_alice_bot`, `writer_alice_bot`. The bot's username must be globally unique on Telegram, so simple names like `research_bot` are almost certainly taken. Suffix with your own handle to avoid collisions.

By the end, you have six tokens. Save them somewhere temporary (a notes file you'll delete) — you're about to put them into each agent's `.env`.

### 4.4 Distribute tokens into each agent's `.env`

This is the bridge between Telegram's side and Hermes's side. Each agent needs two env vars in its host-side `.env` file:

```bash
# On the Mini, for the research agent:
cat >> ~/.hermes-research/.env <<EOF
TELEGRAM_BOT_TOKEN=7123456789:AAH1bGciOiJSUzI1NiIsInR5cCI6Ikp...
TELEGRAM_ALLOWED_USERS=123456789
EOF

# Repeat for each agent — each gets its own bot token, same user ID
```

Important formatting rules:

- **No quotes around the token value.** Telegram tokens contain a colon, which some env parsers misinterpret under quoting. Plain `TOKEN=value` form only.
- **`TELEGRAM_ALLOWED_USERS` is comma-separated.** Single value for a personal setup. If you want a friend to also be able to talk to a specific bot, add their user ID separated by a comma (no spaces).
- **Permissions on the `.env` file.** `chmod 600 ~/.hermes-*/.env` so only your user can read them. The tokens are bearer credentials.

### 4.5 Per-agent config

In each agent's `~/.hermes-<name>/config.yaml`, enable the Telegram gateway:

```yaml
gateway:
  enabled: true
  platforms:
    telegram:
      enabled: true
      extra:
        disable_link_previews: true   # cleaner output for agents that share links
```

The token and allowed-users values are read from `.env` automatically — they don't need to be repeated here.

### 4.6 Token collision protection (Hermes-side)

Hermes detects if two agents accidentally use the same bot token and refuses to start the second one. *"If two profiles accidentally use the same bot token, the second gateway will be blocked with a clear error naming the conflicting profile."* Same protection applies across containers — first-write wins on a shared token, second one gets an explicit failure rather than silently competing.

If you ever see `Token already in use by hermes-<other>` in logs, two of your agents have the same token. Most common cause: copy-paste error in step 4.4. Re-issue a fresh token for one bot via `/revoke` in BotFather and update the right `.env`.

### 4.7 Verify each bot before starting the agent

Before bringing the agent container up, sanity-check the bot exists on Telegram's side:

```bash
TOKEN="7123456789:AAH..."
curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | jq
```

Expected output includes `"ok": true` and the bot's username. If you get `"ok": false` or 401, the token is wrong — re-copy from BotFather.

Do this for each of the six tokens. Takes 30 seconds total and catches typos before they cause weird container startup failures.

### 4.8 First contact after bringing the agent up

After `docker compose up -d hermes-research`, open Telegram, find your bot (search for its username, or use the t.me/<username> link BotFather gave you), and send `/start` or any message. It should reply within a few seconds.

If it doesn't reply:

```bash
docker logs --tail 100 hermes-research | grep -i telegram
```

Common issues and fixes:

- **`401 Unauthorized`** — token wrong or revoked. Re-issue via BotFather.
- **`Conflict: terminated by other getUpdates request`** — another process is polling the same bot. Find it (`docker ps | grep <agent>` or check launchd) and stop the duplicate.
- **No log line about Telegram at all** — gateway not enabled in `config.yaml`. Recheck step 4.5.
- **`TELEGRAM_ALLOWED_USERS not configured, denying`** — user ID missing or wrong. Check `.env`, verify with @userinfobot.

### 4.9 Optional: set bot commands and home channel

**Bot commands** show a `/` menu in the Telegram client. From BotFather:

```
/mybots → pick the bot → Edit Bot → Edit Commands
```

Paste this for each agent (or tailor per agent):

```
new - Start a fresh conversation
status - Show session info
stop - Stop the current task
help - Show available commands
```

**Home channel for cron output.** Hermes can schedule jobs (via the `cron/` directory) that deliver output to a Telegram chat. The default is wherever you last set with `/sethome`. In each bot's DM, type:

```
/sethome
```

That marks the current chat as where the agent should send scheduled outputs. Useful for `concierge` (morning digest), `ops` (status reports), and `research` (cron'd briefings).

### 4.10 Bot-to-agent mapping reference

Seven bots, seven agents. Display names are the short Turkic forms; the full honorific form plus the role go in each bot's profile (Section 4.9 `/setdescription` and `/setabouttext`). Fill the token column as you create each bot. Replace `<you>` with your Telegram handle; usernames are ASCII, lowercase, and must be globally unique (`Ülgen` → `ulgen`).

| Slug | Display (chat) | Username | Token (first 10) | Full name | Role at a glance |
|---|---|---|---|---|---|
| `general` | Kam | `kam_<you>_bot` | `…` | Kam Ata | Talk about anything — your main line |
| `research` | Mergen | `mergen_<you>_bot` | `…` | Mergen Han | Research any topic + game scout |
| `concierge` | Umay | `umay_<you>_bot` | `…` | Umay Ana | Daily life, reminders, digest |
| `ops` | Asena | `asena_<you>_bot` | `…` | Asena Ana | Watches the system |
| `coder` | Ülgen | `ulgen_<you>_bot` | `…` | Bay Ülgen | Writes & runs code |
| `writer` | Korkut | `korkut_<you>_bot` | `…` | Dede Korkut | Drafts & edits prose |
| `producer` | Kayra | `kayra_<you>_bot` | `…` | Kayra Han | Scores game ideas (Phase B) |

Save this. Seven similar bots in a chat list blur together without notes.

**Telegram profile text — paste per bot in BotFather.** `/setabouttext` is the short line in the bot's info card; `/setdescription` is the longer blurb shown on the empty-chat screen. These double as your "which agent does what" reference.

```
Kam      (general)
  about:       Kam Ata — your shaman. Ask anything.
  description: Father Shaman. Your main line: open conversation, brainstorming,
               quick answers, and hand-offs to the specialists. Talk about anything.

Mergen   (research)
  about:       Mergen Han — research, any topic.
  description: Lord of wisdom. Researches any domain — game markets, history,
               academic sources. Cites every source. Runs the weekly game scout.

Umay     (concierge)
  about:       Umay Ana — daily life & digests.
  description: Mother of the hearth. Calendar, reminders, and your morning digest.
               Warm, brief, action-first.

Asena    (ops)
  about:       Asena Ana — watches the system.
  description: The wolf-mother, ever vigilant. Monitors the agent fleet and host,
               sends status reports, runs scheduled checks. Terse and factual.

Ülgen    (coder)
  about:       Bay Ülgen — writes & runs code.
  description: The maker. Your development pair: Godot-first game code, refactors,
               debugging. Direct, shows diffs, tests its own work.

Korkut   (writer)
  about:       Dede Korkut — drafts & edits.
  description: The legendary bard. Drafts, edits, brainstorms — prose, store copy,
               game PRDs. Playful and generative.

Kayra    (producer)
  about:       Kayra Han — scores game ideas.
  description: The creator. Game-dev discovery: keeps the opportunity backlog and
               scores ideas against the rubric. (Activates in Phase B.)
```

### 4.11 Things to know about Telegram + Hermes specifically

A few details that matter for our setup:

**Voice memos auto-transcribe.** If you record a voice message in Telegram, Hermes transcribes it via the configured STT provider and processes it as a normal message. For local-only transcription with no API key, `stt.provider: local` uses `faster-whisper` on the machine running Hermes. Install with `pip install faster-whisper` in the container — or just leave it at the default and accept that the first voice message in each session takes longer while the model downloads.

**TTS replies are native voice bubbles.** If an agent uses `text_to_speech`, the result is delivered as Telegram's inline-playable voice format, not a file attachment. Configure per-agent in `config.yaml` under `tts:` — Edge TTS is free and decent quality.

**Markdown tables get auto-flattened.** Telegram's MarkdownV2 doesn't support tables. Hermes detects them and either flattens small tables into bulleted lists or wraps larger ones in code blocks. You don't need to configure anything — the adapter handles it.

**File attachments require host-readable paths.** When the agent sends a file via `MEDIA:/...`, the path must be readable on the host where the gateway runs, not inside the container. Since our gateway runs *inside* the container, this means the file has to be in a host-mounted volume. For `writer`, that's why we set up `~/Documents/writer-output:/output` — files written to `/output/` inside the container are visible at `~/Documents/writer-output/` on the host, and that's the path the gateway can read.

**Group chats are off by default.** With `/setjoingroups Disable` from step 4.3, the bots can't be added to groups at all. If you ever want a bot in a group (probably `concierge` for a family group), re-enable `/setjoingroups` and also set the per-platform `group_allow_from` allowlist in `config.yaml` to restrict who can invoke the bot.

**Single Telegram account, seven bots, no conflict.** Telegram supports unlimited bots per account. Bots are independent of the account that created them — each has its own identity, its own token, and shows up as a separate chat. Your seven bots will appear as seven separate conversations in your chat list.

### 4.12 Shortcut — automate everything after bot creation

Bot *creation* (4.3) is the only manual part; Telegram has no API for it. Once the seven bots exist and you have their tokens, `setup-bots.sh` (in `~/Desktop/hermes-setup/`) does the rest in one command — it replaces the hand-work in **4.4** (token → `.env`) and **4.9** (commands, description, about text).

```bash
# from ~/Desktop/hermes-setup/
cp bot-tokens.env.example bot-tokens.env && chmod 600 bot-tokens.env
# edit bot-tokens.env: paste ALLOWED_USERS (your @userinfobot ID) + the 7 tokens
./setup-bots.sh
```

For each bot it calls the Bot API (`setMyName`, `setMyShortDescription`, `setMyDescription`, `setMyCommands`) with the names and profile text from 4.10, then writes `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USERS` into `~/.hermes-<slug>/.env` — **non-destructive** (preserves any model-provider keys already in the file), `chmod 600` — and verifies each token with `getMe`. Idempotent: rerun anytime to update the profile text.

Still manual afterward (BotFather-only, ~10 s per bot): `/setprivacy` → Disable and `/setjoingroups` → Disable (the hardening from 4.3).

The token you paste into `bot-tokens.env` is the same value each agent's `.env` needs — the script wires both Telegram's side and the agents' side from that one entry, so you never paste a token twice.

---

## 5. Model providers per agent

Six agents, three providers. Codex OAuth for the conversational agents (`research`, `concierge`'s text generation, `writer`), Anthropic API key for the coders (`coder`), MiniMax API key for the workhorses where MiniMax-M2.7 is competitive at lower cost (`concierge` reasoning, `ops`). One cheap auxiliary model across all six for vision and summarization to keep token costs in check.

### 5.1 Per-agent model assignment

| Agent | Main model | Provider | Why |
|---|---|---|---|
| `research` | `gpt-5.3` (Codex) | `openai-codex` | Long-context reasoning, web research depth, citation discipline |
| `concierge` | `MiniMax-M2.7` | `minimax` | Strong reasoning at lower cost than GPT-5; daily-use volume |
| `ops` | `MiniMax-M2.7-highspeed` | `minimax` | Fast, cheap, deterministic — `ops` doesn't need depth |
| `coder` | `claude-sonnet-4-6` | `anthropic` | Best coding model with full agent tool support |
| `writer` | `gpt-5.3` (Codex) | `openai-codex` | Voice, editorial nuance, long-form drafting |
| *(spare)* | TBD | TBD | Pick when role is decided |

For all six, the **auxiliary model** (vision, web summarization, context compression, session search) is `google/gemini-2.5-flash` via OpenRouter. Reasoning on this is in 5.6 below — short version: aux tasks fire often, are short, and don't need the main model's depth, so routing them to the cheapest fast model saves real money.

### 5.2 Codex OAuth setup — the two paths

Codex uses OAuth against your ChatGPT account. No API billing — your existing ChatGPT subscription pays for it. The token gets saved to `~/.hermes-<name>/auth.json` per agent and persists across container restarts via the bind mount.

The friction is bootstrapping the OAuth for three separate containers (`research`, `concierge`'s text gen if you want it on Codex, and `writer`). Two paths.

#### Path A — Import existing Codex credentials (recommended if you already use ChatGPT Desktop or Codex CLI)

If you have the Codex CLI installed on your Mac or are signed into ChatGPT Desktop, your credentials live at `~/.codex/auth.json`. Hermes auto-imports these on first container startup.

For each Codex-using agent, copy the host's Codex auth file into the agent's data directory **before** first container start:

```bash
# On the M4 Mini, for research agent
mkdir -p ~/.hermes-research
cp ~/.codex/auth.json ~/.hermes-research/auth.json
chmod 600 ~/.hermes-research/auth.json
```

Repeat for the MacBook's `writer` agent. The credentials are now scoped to each agent's data directory; refresh works automatically.

Update each agent's `config.yaml`:

```yaml
model:
  provider: openai-codex
  default: gpt-5.3
```

That's it. Container starts, Hermes finds `/opt/data/auth.json`, uses it.

#### Path B — Fresh OAuth inside each container (if you don't have Codex credentials yet)

For each Codex-using agent, run the setup wizard interactively before background mode:

```bash
docker run -it --rm \
  -v ~/.hermes-research:/opt/data \
  nousresearch/hermes-agent setup
```

When the wizard reaches the model step, pick **"OpenAI Codex"**. The container prints a URL and a device code. Open the URL on your Mac, paste the code, approve in browser. Hermes writes the token to `/opt/data/auth.json` (= `~/.hermes-research/auth.json` on the host).

Repeat for each Codex-using agent. Three separate device-code flows. Annoying but one-time.

#### Refresh and credential expiry

Codex OAuth tokens refresh automatically — Hermes deduplicates concurrent refreshes and writes atomically. If you ever see `invalid_grant` errors in agent logs, that means the refresh token itself was revoked (likely a password change or remote signout on the ChatGPT side). Fix: redo Path A or B for that agent.

#### One Codex account, three agents

Important: all three Codex-using agents share the same underlying ChatGPT account quota. ChatGPT Plus ($20/mo) and Pro ($200/mo) have different daily message caps. Three active agents hammering at Codex simultaneously can chew through Plus's daily limit. If you find yourself rate-limited, options are: switch some agents to a different provider, queue heavy tasks via cron, or upgrade to Pro.

### 5.3 Anthropic API key setup for `coder`

Simplest of the three. Get an API key at console.anthropic.com → Settings → API Keys.

Add to `coder`'s `.env`:

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> ~/.hermes-coder/.env
```

In `~/.hermes-coder/config.yaml`:

```yaml
model:
  provider: anthropic
  default: claude-sonnet-4-6
```

That's it. No OAuth, no device codes. The API key is pay-per-token via your Anthropic console — track usage at console.anthropic.com.

#### Sonnet vs Opus

Default is Sonnet 4.6 — fast, capable, ~70% the cost of Opus for most coding work. Switch to Opus for hard refactors or architecture work via `/model claude-opus-4-6` inside a session. The model lives in the same provider, so no separate setup needed.

#### Why not Codex for coding too?

Codex is genuinely good at code generation, but Claude has stronger agent-tool discipline — better at calling tools correctly, handling multi-step refactors, and staying focused across long sessions. For an agent that needs to run terminal commands, edit files, and verify its own work, Claude's edge matters. Spend the API tokens for the agent that needs them most.

#### If you decide later to switch to Claude Max + credits

The path exists: `hermes model` → Anthropic OAuth, requires Claude Max ($100/mo) plus separately purchased "extra usage" credits (not Pro). Worth considering if your Anthropic API bill exceeds $100/mo. For now, API key is the right call.

### 5.4 MiniMax API key setup for `concierge` and `ops`

Get an API key at platform.minimax.io. Two regional endpoints exist:

- **Global** (`minimax`, `api.minimax.io`) — what to use unless you're in mainland China.
- **China** (`minimax-cn`, `api.minimaxi.com`) — alternative for China-region accounts.

Add to each MiniMax-using agent's `.env`:

```bash
echo "MINIMAX_API_KEY=mn_..." >> ~/.hermes-concierge/.env
echo "MINIMAX_API_KEY=mn_..." >> ~/.hermes-ops/.env
```

You can use the same API key across both agents — MiniMax bills per token, no per-key restrictions.

In `~/.hermes-concierge/config.yaml`:

```yaml
model:
  provider: minimax
  default: MiniMax-M2.7
```

In `~/.hermes-ops/config.yaml`:

```yaml
model:
  provider: minimax
  default: MiniMax-M2.7-highspeed
```

`-highspeed` is MiniMax's faster, cheaper variant — ideal for `ops` where you want quick deterministic responses, not deep reasoning.

#### Why not MiniMax OAuth?

MiniMax also offers a browser-OAuth path (`minimax-oauth`) with a free tier — same model, no API billing. The trade-off is per-container OAuth complexity (same as Codex) and a more limited rate cap on the free tier. For two persistent gateway agents (`concierge` runs cron jobs, `ops` runs monitoring), an API key is more reliable than OAuth tokens that can rate-limit during heavy use.

If cost becomes a concern, flip to `minimax-oauth` — change `provider: minimax` to `provider: minimax-oauth` in the config and run the OAuth flow inside the container.

### 5.5 OpenRouter for auxiliary tasks (one key, all six agents)

Auxiliary tasks fire constantly: every time an agent uses vision, summarizes a web page, generates a session title, or compresses old context. Hermes defaults these to the main chat model unless you override. For agents on expensive main models (Claude Opus, GPT-5), this adds up fast.

The fix: route aux tasks to a cheap fast model via OpenRouter. Gemini Flash 2.5 at ~$0.075/M input tokens is roughly **100x cheaper** than Claude Sonnet for the same call. Same key works for all six agents.

Get an OpenRouter key at openrouter.ai → Keys. Add $5–10 of credit; for personal multi-agent use this lasts months.

Add the key to **every** agent's `.env` (Mini and MacBook):

```bash
for agent in research concierge ops; do
  echo "OPENROUTER_API_KEY=sk-or-..." >> ~/.hermes-${agent}/.env
done

# On the MacBook
for agent in coder writer; do
  echo "OPENROUTER_API_KEY=sk-or-..." >> ~/.hermes-${agent}/.env
done
```

Then in **every** agent's `config.yaml`, override auxiliary tasks:

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

That's the entire override. Main chat stays on Codex/Claude/MiniMax; everything else routes through cheap Gemini Flash.

#### Why Gemini Flash, not something else?

- **Cost** — among the cheapest models with usable agent-tool reliability.
- **Speed** — sub-second latency on aux tasks keeps the agent feeling responsive.
- **Multimodal** — handles vision for image analysis, where many cheap models can't.
- **Generous OpenRouter rate limits** — won't bottleneck six agents calling concurrently.
- **Doesn't add a new account** — you already need OpenRouter for fallback (5.7), so this is a free addition.

Alternative if you want to use MiniMax-highspeed for aux (one fewer provider): change every `provider: openrouter` / `model: google/gemini-2.5-flash` pair above to `provider: minimax` / `model: MiniMax-M2.7-highspeed`. Same key as `concierge`/`ops`. Slightly more expensive than Gemini Flash but consolidates providers. Either works.

### 5.6 Cost ballpark (per month, personal use)

Rough sketch assuming moderate daily use of all six agents. Real numbers will vary.

| Agent | Main provider | Estimate |
|---|---|---|
| `research` | Codex (ChatGPT Plus) | $0 — included in subscription |
| `concierge` | MiniMax API | $2–8/mo at moderate daily use |
| `ops` | MiniMax API (highspeed) | $1–3/mo |
| `coder` | Anthropic API (Sonnet) | $5–30/mo depending on active dev hours |
| `writer` | Codex (ChatGPT Plus) | $0 — included |
| *Auxiliary (all six)* | OpenRouter (Gemini Flash) | $1–4/mo |
| **Total** | | **~$9–45/mo** plus your existing ChatGPT Plus ($20) |

The variability is mostly `coder` — heavy refactoring days can spike. If you find Anthropic API bills regularly above $50/mo, that's the signal to consider Claude Max + credits.

If you don't have ChatGPT Plus and don't want to pay $20/mo for it, replace Codex with OpenAI API key (provider: `openai`, set `OPENAI_API_KEY`) — pay-per-token via openai.com. Estimate $5–15/mo for `research` + `writer` combined at moderate use.

### 5.7 Fallback chains per agent

Hermes supports a fallback provider chain — if the main model fails (rate limit, server error, auth failure), it tries the next entry without losing the conversation. Configure once, gain resilience.

Pattern per agent — pick a fallback that's a different provider so a single-provider outage doesn't take everything down.

`research` (`config.yaml`):
```yaml
fallback_providers:
  - provider: openrouter
    model: openai/gpt-5
  - provider: openrouter
    model: anthropic/claude-sonnet-4-6
```

`writer` (`config.yaml`):
```yaml
fallback_providers:
  - provider: openrouter
    model: openai/gpt-5
  - provider: openrouter
    model: google/gemini-2.5-pro
```

`coder` (`config.yaml`):
```yaml
fallback_providers:
  - provider: openrouter
    model: anthropic/claude-sonnet-4-6
  - provider: openrouter
    model: openai/gpt-5
```

`concierge` (`config.yaml`):
```yaml
fallback_providers:
  - provider: openrouter
    model: minimaxai/minimax-m2.7
  - provider: openrouter
    model: google/gemini-2.5-flash
```

`ops` (`config.yaml`):
```yaml
fallback_providers:
  - provider: openrouter
    model: google/gemini-2.5-flash
```

The OpenRouter key you set up in 5.5 covers all of these — no extra credentials.

#### What a fallback actually does

If `coder`'s primary Anthropic call returns 503 or hits a rate limit, Hermes immediately retries the same request against `openrouter` with Claude Sonnet (a separate provider that proxies to Anthropic with its own routing pool). If that also fails, it falls to OpenAI GPT-5 via OpenRouter. The session continues without the user knowing anything happened. This is one of the most underrated features of Hermes — your agents don't break when one provider has a bad day.

### 5.8 Putting it together: per-agent `.env` summary

For reference when wiring up each agent.

`~/.hermes-research/.env`:
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
OPENROUTER_API_KEY=sk-or-...        # aux + fallback
# Codex credentials in auth.json (not .env)
```

`~/.hermes-concierge/.env`:
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
MINIMAX_API_KEY=mn_...
OPENROUTER_API_KEY=sk-or-...
```

`~/.hermes-ops/.env`:
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
MINIMAX_API_KEY=mn_...
OPENROUTER_API_KEY=sk-or-...
```

`~/.hermes-coder/.env`:
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
ANTHROPIC_API_KEY=sk-ant-...
OPENROUTER_API_KEY=sk-or-...
```

`~/.hermes-writer/.env`:
```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=...
OPENROUTER_API_KEY=sk-or-...
# Codex credentials in auth.json
```

`chmod 600` on every `.env` after writing. Bearer credentials, treat them like passwords.

### 5.9 Verification

Before starting an agent, sanity-check each provider is reachable. From the host:

```bash
# OpenRouter (works for all aux + fallback)
curl -s https://openrouter.ai/api/v1/models \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | jq '.data | length'
# Expect a number > 200

# Anthropic API
curl -s https://api.anthropic.com/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
# Expect a JSON response with "content"

# MiniMax
curl -s https://api.minimax.io/v1/text/chatcompletion_v2 \
  -H "Authorization: Bearer $MINIMAX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"MiniMax-M2.7-highspeed","messages":[{"role":"user","content":"hi"}],"max_tokens":10}'
# Expect a JSON response with "choices"
```

Codex is harder to verify outside a Hermes container because the auth flow is bound to Hermes's credential store. The verification happens on first agent startup — if Codex auth is broken, the container will fail loudly with "invalid_grant" or "auth required" in logs.

### 5.10 Changing providers later

Every agent's `config.yaml` is the source of truth. Switch a provider at any time:

```bash
# Edit the file
nano ~/.hermes-research/config.yaml
# Change model.provider and model.default

# Restart that agent
docker compose restart hermes-research
```

The agent picks up the new provider on next session. Conversation history, memory, skills all survive — those are provider-agnostic.

Use `/model <new-model>` inside an active chat session to switch *temporarily* without editing the file. Persistent changes need the config edit.

---

## 6. Deployment plan — phased

Do not stand up all six at once. Build in three phases so problems get isolated as they arise.

**Prerequisites:** Section 4 done (six bots exist, six tokens saved, your user ID in hand) and Section 5 done (API keys obtained from Anthropic, MiniMax, OpenRouter, and Codex credentials available either via existing `~/.codex/auth.json` or you're ready to do device-code OAuth inside containers). Deployment assumes both are complete — keys and tokens get pasted during the per-agent setup wizard.

### Phase 1 — first agent end-to-end (day 1)

Pick one agent (recommend `research` — gateway-mode, single platform, no project mounts) and get it fully working before touching anything else.

**Step 1.1: Create the host directory**

```bash
mkdir -p ~/.hermes-research
```

**Step 1.2: Run the setup wizard interactively**

```bash
docker run -it --rm \
  -v ~/.hermes-research:/opt/data \
  nousresearch/hermes-agent setup
```

Configure: the model provider for this agent (per Section 5.1 — `research` uses Codex OAuth), the relevant API keys from Section 5, and the Telegram bot token from Section 4. The wizard writes everything to `~/.hermes-research/.env`. If you already populated `.env` and `auth.json` manually per Section 5's per-agent summary, the wizard will detect existing values and let you skip those prompts.

**Step 1.3: Edit the SOUL.md**

```bash
nano ~/.hermes-research/SOUL.md
```

Write the personality. Example for research:

```
You are a methodical research assistant. Your job is to find primary sources,
synthesize them faithfully, and surface what you don't know. Cite every
non-trivial claim with the source URL. Prefer two reliable sources over one.
When the evidence is mixed, say so explicitly — do not flatten disagreement.
Default to concise output; expand only when asked.
```

**Step 1.4: Start in gateway mode**

```bash
docker run -d \
  --name hermes-research \
  --restart unless-stopped \
  --memory=3g --cpus=1 \
  --shm-size=1g \
  -v ~/.hermes-research:/opt/data \
  -p ${TAILSCALE_IP}:8642:8642 \
  nousresearch/hermes-agent gateway run
```

The `-p ${TAILSCALE_IP}:8642:8642` syntax binds the port to the Mac's Tailscale interface only — not `0.0.0.0`, not the LAN. The port is reachable from your tailnet devices and from nowhere else. Find your Tailscale IP with `tailscale ip -4` (it'll be `100.x.y.z`). See Section 8 for full Tailscale setup.

The `--shm-size=1g` flag is required if you want browser tools to work (Playwright/Chromium needs shared memory).

**Step 1.5: Verify**

```bash
docker ps                                  # container is running
docker logs --tail 50 hermes-research      # no errors at startup
docker stats hermes-research               # memory and CPU within limits
```

Then send a Telegram message to the bot. Confirm it responds. Confirm `~/.hermes-research/sessions/` has a new session file. Leave it running overnight and check it's still healthy in the morning.

**Acceptance criteria for Phase 1:**
- Gateway stays up for 24+ hours without crashing
- Memory stays within the 3 GB cap (`docker stats` confirms)
- A skill gets created and persists across container restarts
- A new session can pick up context from a memory file written in an earlier session

### Phase 2 — second agent, validate isolation (day 2–3)

Stand up `concierge`. The point of this phase is to **prove isolation works** before scaling further.

**Step 2.1: Replicate the pattern**

```bash
mkdir -p ~/.hermes-concierge

docker run -it --rm \
  -v ~/.hermes-concierge:/opt/data \
  nousresearch/hermes-agent setup
```

Use a **different Telegram bot token** for concierge. Edit its SOUL.md with a different personality.

**Step 2.2: Start with a different port**

```bash
docker run -d \
  --name hermes-concierge \
  --restart unless-stopped \
  --memory=2g --cpus=1 \
  --shm-size=1g \
  -v ~/.hermes-concierge:/opt/data \
  -p ${TAILSCALE_IP}:8643:8642 \
  nousresearch/hermes-agent gateway run
```

**Step 2.3: Run the isolation test**

This is the non-negotiable check. From a chat with `research`, ask the agent to create a file at `/opt/data/marker-research.txt`. From a chat with `concierge`, ask it to list files in `/opt/data/`. The marker file should not appear — concierge sees its own `/opt/data`, which is a different host directory.

If `concierge` can see `research`'s files, something is wrong with the bind mounts and the entire isolation story collapses. Fix before continuing.

**Step 2.4: Cross-restart test**

`docker restart hermes-research`. Confirm `concierge` keeps running and responding normally throughout. This validates the independent-lifecycle claim.

**Acceptance criteria for Phase 2:**
- Both gateways run simultaneously without conflict
- Filesystem isolation confirmed (marker test passes)
- Lifecycle isolation confirmed (restart test passes)
- Different bot tokens, different personalities, both reachable

### Phase 3 — remaining agents (week 1+)

Once Phase 2 passes, the remaining four are mechanical. Copy the pattern, change names, ports, RAM caps, SOUL.md.

For each new agent:
1. Create `~/.hermes-<name>/` directory
2. Run interactive setup with that directory as the data mount
3. Write the SOUL.md
4. Configure a unique bot token
5. `docker run -d` with the right name, port, and resource caps

**Special considerations per agent:**

- **`coder` (MacBook Pro):** Add a volume mount for your projects directory. Set `docker_run_as_host_user: true` equivalent by passing `--user $(id -u):$(id -g)` so created files are owned by you, not root. Example:

  ```bash
  docker run -d \
    --name hermes-coder \
    --restart unless-stopped \
    --memory=4g --cpus=2 \
    --shm-size=1g \
    --user $(id -u):$(id -g) \
    -v ~/.hermes-coder:/opt/data \
    -v ~/projects:/workspace/projects \
    -p ${TAILSCALE_IP}:8645:8642 \
    nousresearch/hermes-agent gateway run
  ```

- **`ops`:** Tightest resource caps. Probably doesn't need browser tools, so `--shm-size` can be smaller or omitted. Consider disabling browser toolset entirely in its `config.yaml`:

  ```yaml
  agent:
    disabled_toolsets:
      - browser
      - web
  ```

- **`writer`:** Add a `/output` volume mount for finished drafts you want to retrieve from the host:

  ```bash
  -v ~/Documents/writer-output:/output
  ```

### 6.6 Toolset hygiene — disable what each agent doesn't need

Hermes loads **~17 toolsets by default**, and each enabled toolset injects its tool schemas into *every* prompt that agent sends. Pruning them is the single cheapest lever you have on three things at once: **token cost** (fewer schemas per prompt), **risk surface** (`terminal`, `code_execution`, `browser` = shell, arbitrary code, full browser automation), and **focus** (fewer tempting wrong tools, which matters most for `ops`).

First, separate three things people conflate:

| Thing | What it is | Token cost | What to do |
|---|---|---|---|
| **Toolsets** | Capabilities injected into the system prompt | **High** — schemas sit in every prompt | **Prune per agent (this section)** |
| **Skills** | On-demand knowledge docs (progressive disclosure) | ~0 until the agent opens one | Don't prune; only *add* useful ones (e.g. the TinyFish skill, 10.7) |
| **Plugins** | Memory providers / context engines / model providers / platform adapters | Pick one of each | Almost nothing to disable — you pick **Honcho** as the memory provider (Section 9); the rest auto-load |

So nearly all the "disable unneeded" effort is **toolsets**. Skills are lazy-loaded and effectively free; plugins are a one-pick decision already made.

**The high-value disables** (all default-on, rarely needed):

- **`browser`** — heavy (≈10 tools, needs Chromium + `--shm-size=1g`). We use **TinyFish via MCP** (Section 10) for web. Disable browser on every agent → this also lets you **drop `--shm-size=1g`** from those containers and reclaim the RAM. Re-enable browser (and shm) only on an agent that genuinely needs interactive page automation (none do, at first).
- **`terminal`** — shell access. Only `coder` and `ops` need it.
- **`code_execution`** — runs Python. Only `coder`.
- **`image_gen`** — default-on but requires a FAL.ai key you don't have, so it's dead schema weight. Disable everywhere.
- **`delegation`** — default-on, spawns subagents = surprise token spend. Disable until you deliberately want multi-agent fan-out.

**Per-agent matrix** (core toolsets always kept — `memory`, `session_search`, `skills`, `clarify`, `safe` — are omitted; `ops` keeps `memory` tight per 9.4):

| Toolset | research | concierge | ops | coder | writer |
|---|:--:|:--:|:--:|:--:|:--:|
| `terminal` | ✗ | ✗ | ✓ | ✓ | ✗ |
| `code_execution` | ✗ | ✗ | ✗ | ✓ | ✗ |
| `browser` | ✗ | ✗ | ✗ | ✗ | ✗ |
| `web` (built-in) | ✓ fallback | ✓ fallback | ✗ | ✗ | ✗ |
| `file` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `vision` | ✓ | ✓ | ✗ | ✓ | ✓ |
| `image_gen` | ✗ | ✗ | ✗ | ✗ | ✗ |
| `tts` | ✗ | ✓ | ✗ | ✗ | ✗ |
| `cronjob` | ✓ | ✓ | ✓ | ✗ | ✗ |
| `messaging` | ✗ | ✗ | ✗ | ✗ | ✗ |
| `delegation` | ✗ | ✗ | ✗ | opt | ✗ |
| `todo` | ✗ | ✗ | ✗ | ✓ | ✗ |

The `web` row matches the decisions already made elsewhere: `ops` kills web (Section 6 special-considerations), `coder`/`writer` are TinyFish-only (Section 10.6, Layer 1), `research`/`concierge` keep built-in web as the SearXNG fallback (Section 10.6, Layer 2).

**Per-agent `disabled_toolsets`** (paste into each `config.yaml`):

```yaml
# general (Kam)  — keeps web/vision/tts/cronjob; it's a conversational agent
agent:
  disabled_toolsets: [terminal, code_execution, browser, image_gen, delegation, messaging, todo]

# research
agent:
  disabled_toolsets: [terminal, code_execution, browser, image_gen, tts, delegation, messaging, todo]

# concierge
agent:
  disabled_toolsets: [terminal, code_execution, browser, image_gen, delegation, messaging, todo]

# ops  (determinism — keep it lean; terminal + cronjob are its job)
agent:
  disabled_toolsets: [browser, web, image_gen, vision, tts, code_execution, delegation, messaging, todo, session_search]

# coder  (the only agent with shell + code execution)
agent:
  disabled_toolsets: [browser, web, image_gen, tts, cronjob, messaging, delegation]

# writer
agent:
  disabled_toolsets: [terminal, code_execution, browser, web, image_gen, cronjob, messaging, delegation, todo]
```

Opt-in toolsets (`search`, `video`, `video_gen`, `moa`, `kanban`, `debugging`, `computer_use`, `homeassistant`, `spotify`, `discord`, `feishu_doc`, `feishu_drive`, `yuanbao`, `x_search`) are **already off** — do not list them. Enable an integration only when you specifically want it (e.g. `homeassistant` on `concierge` later if you run a smart home; needs `HASS_TOKEN`).

Verify the live result per agent with:

```bash
hermes tools --list   # shows active toolsets vs available-but-disabled
```

**`producer` (Phase B, deferred):** when you build it, disable `[terminal, code_execution, browser, web, image_gen, vision, tts, delegation]`. It keeps `file` (writes `backlog.md`), `cronjob` (weekly scoring), plus the core memory/skills/clarify set. Nothing heavier.

**Two flags worth remembering:**

- **Drop `--shm-size=1g` once browser is disabled.** Sections 6 and 7 set `shm_size: 1gb` on agents for browser support. With `browser` disabled everywhere, that shared-memory allocation is wasted — remove it from each container's `docker run`/compose entry to reclaim RAM. Add it back only on an agent where you later re-enable `browser`.
- **`ops` `terminal` sees the container, not the host.** A containerized `ops` agent's shell operates inside its own container — it cannot see host disk usage, the Docker daemon, or other containers without explicit host access (a mounted Docker socket or a host-metrics path). Before relying on `ops` for real infrastructure monitoring, decide how it reaches the host; the `terminal` toolset alone only gives it visibility into its own sandbox.

**Phase A relevance:** only `research` is live at first, so its disable list is the only one you need on day one. The rest apply as each agent comes online.

### 6.7 Agent SOULs — Turkic, seasoned (function-first)

Each agent's `~/.hermes-<slug>/SOUL.md`. The style is **seasoned, not cosplay**: the Turkic flavor and the actual instruction are the *same sentence*, so the persona reinforces good behavior without spending tokens on theatre. Keep them lean; edit anytime. These supersede the short inline examples earlier in the plan.

**`general` — Kam:**
```
You are Kam, a shaman — the one consulted about anything. Calm, broad, genuinely
curious. Help with whatever is asked: think out loud, brainstorm, answer plainly,
or sit with an open question. A little dry wit is welcome. You know the other
agents — Mergen researches, Ülgen codes, Korkut writes, Umay runs daily life,
Asena watches the system, Kayra scores game ideas. When a task clearly belongs to
one of them, say so and offer to hand it off. Concise by default; go deep when
asked. Track what matters about the user across conversations.
```

**`research` — Mergen:**
```
You are Mergen, lord of wisdom — and a tracker at heart. Follow trails to their
source; report what you find, never what you guessed. Cite every trail you walk
with its URL. Two solid sources beat one. When sources conflict, show both — do
not flatten disagreement. Surface what you do not know. Match method to domain:
primary sources and dates for history, peer-reviewed for academic, charts and
review-mining for game markets (load the matching research skill). Concise by
default; expand on request.
```

**`concierge` — Umay:**
```
You are Umay, mother of the hearth. You keep the user's day running: calendar,
reminders, the morning digest. Warm but brief — say what matters, then stop. Act
first, ask only when truly unsure. Protect their attention: ping immediately only
for what is time-sensitive; queue the rest for the digest. Never pad.
```

**`ops` — Asena:**
```
You are Asena, the wolf-mother — ever watchful. Report what you see, nothing you
do not. Terse, status-bar voice; numbers over adjectives. Check, summarize, surface
anomalies plainly. Sound the alarm only when it is real. Do exactly what is asked —
no embellishment, no drift. You see only your own container unless given host
access; say so rather than guessing about the host.
```

**`coder` — Ülgen:**
```
You are Ülgen, the maker. Measure twice, strike once. Direct, opinionated,
code-first. Show diffs. Run and test your own work before calling it done; report
failures honestly with the output. Match the surrounding code's style. Godot and
GDScript are the default forge for game work. Working steel over pretty rust — no
ornament the task did not ask for.
```

**`writer` — Korkut:**
```
You are Korkut — Dede Korkut, the bard. You draft, edit, and brainstorm: prose,
copy, game PRDs. Playful and generative; offer options, not one timid draft. Lead
with the core idea, then shape it. Keep PRDs lean — two pages, the loop and the
player first. Match the user's voice; when editing, preserve their intent and cut
only what weakens it.
```

**`producer` — Kayra (Phase B):**
```
You are Kayra, the creator — a skeptical game-dev product lead. Keep an honest
opportunity backlog. Score candidates against the rubric: buildable, loop clarity,
discovery, monetization. Never inflate a score to be encouraging. Never judge "fun"
or taste — that is the user's call; surface the trade-offs and stop. Bias toward
small, solo-buildable scope. Kill stale ideas without sentiment. For each top
candidate: a one-sentence pitch and the single biggest risk.
```

---

## 7. Docker Compose — recommended for managing seven containers

Once Phase 3 is underway, six `docker run` commands become annoying to maintain. Consolidate into one `docker-compose.yaml` per machine.

### M4 Mini `~/hermes/docker-compose.yaml`

Set `TAILSCALE_IP` once in a `.env` file alongside the compose file (`echo "TAILSCALE_IP=$(tailscale ip -4)" > ~/hermes/.env`), then Compose substitutes it into every port binding.

```yaml
services:
  hermes-research:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-research
    restart: unless-stopped
    command: gateway run
    ports:
      - "${TAILSCALE_IP}:8642:8642"
    volumes:
      - ~/.hermes-research:/opt/data
    shm_size: 1gb
    deploy:
      resources:
        limits:
          memory: 3G
          cpus: "1.0"

  hermes-concierge:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-concierge
    restart: unless-stopped
    command: gateway run
    ports:
      - "${TAILSCALE_IP}:8643:8642"
    volumes:
      - ~/.hermes-concierge:/opt/data
    shm_size: 1gb
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: "1.0"

  hermes-ops:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-ops
    restart: unless-stopped
    command: gateway run
    ports:
      - "${TAILSCALE_IP}:8644:8642"
    volumes:
      - ~/.hermes-ops:/opt/data
    deploy:
      resources:
        limits:
          memory: 1G
          cpus: "0.5"
```

### MacBook Pro `~/hermes/docker-compose.yaml`

Same pattern — create a `.env` file with `TAILSCALE_IP` set to the MacBook's tailnet IP.

```yaml
services:
  hermes-coder:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-coder
    restart: unless-stopped
    command: gateway run
    user: "501:20"  # adjust to your host UID:GID — check with `id`
    ports:
      - "${TAILSCALE_IP}:8645:8642"
    volumes:
      - ~/.hermes-coder:/opt/data
      - ~/projects:/workspace/projects
    shm_size: 1gb
    deploy:
      resources:
        limits:
          memory: 4G
          cpus: "2.0"

  hermes-writer:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-writer
    restart: unless-stopped
    command: gateway run
    ports:
      - "${TAILSCALE_IP}:8646:8642"
    volumes:
      - ~/.hermes-writer:/opt/data
      - ~/Documents/writer-output:/output
    shm_size: 1gb
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: "1.0"
```

Operations become simple:

```bash
docker compose up -d              # start all agents on this machine
docker compose logs -f hermes-research   # follow research logs
docker compose restart hermes-coder      # restart just coder
docker compose pull && docker compose up -d   # upgrade all agents
```

---

## 8. Tailscale networking

The whole point of using Tailscale here is the same one Nous Research recommends on their AWS marketplace listing: *"Do not expose application ports publicly; use SSH tunnels, Caddy with HTTPS, or Tailscale."* Hermes has no auth on its gateway API by default, so any port reachable from the public internet is a complete-access port. Tailscale gives you a private mesh — only devices you've explicitly added to your tailnet can reach the agents, and traffic is end-to-end encrypted over WireGuard.

There's an open feature request (issue #9269) for first-class `tailscale serve` integration in Hermes itself, but it isn't shipped yet. Until then, the integration happens at the OS layer: bind ports to the Tailscale interface, done.

### 8.1 Tailscale setup on each Mac

Install Tailscale on both machines via the App Store or `brew install --cask tailscale`. Sign in to the same tailnet on both. Pick stable, readable hostnames in the Tailscale admin console — `hermes-mini` and `hermes-mbp` are more useful than the autogenerated names. Enable MagicDNS in Tailscale admin so you can reach the machines by name (`hermes-mini.your-tailnet.ts.net`) instead of by raw IP.

Verify on each Mac:

```bash
tailscale status                 # confirm node is up, see peers
tailscale ip -4                  # your tailnet IP (100.x.y.z) — save this
```

### 8.2 The bind-to-Tailscale pattern

Docker's `-p` flag accepts a host IP prefix. `-p 100.x.y.z:8642:8642` binds the port to that interface only. Traffic from `localhost`, the LAN, or the internet cannot reach it; only traffic arriving on the Tailscale interface can.

Set the IP once in each machine's `~/hermes/.env` file:

```bash
echo "TAILSCALE_IP=$(tailscale ip -4)" > ~/hermes/.env
```

Compose substitutes `${TAILSCALE_IP}` into every port mapping. All the compose snippets in Section 7 already use this pattern.

### 8.3 Reaching the agents from your phone or other devices

Install Tailscale on your phone (iOS/Android), sign in to the same tailnet, and the agents are immediately reachable from anywhere — home wifi, mobile data, traveling internationally. The mesh handles NAT traversal and routing automatically.

If you want HTTPS for browser-based dashboards (instead of plain HTTP over Tailscale), use `tailscale cert` to issue a real cert for your machine's tailnet name, or use `tailscale serve` to proxy HTTPS in front of the local HTTP port:

```bash
tailscale serve --bg --https=443 http://localhost:9119
```

This is useful if you enable the Hermes dashboard and want to PWA-install it on your phone (which requires HTTPS).

### 8.4 Verifying the bind is correct

After bringing up containers, this check is non-negotiable. From the host:

```bash
# Should show Tailscale IP, NOT 0.0.0.0
sudo lsof -iTCP -sTCP:LISTEN -P -n | grep 864

# Or:
netstat -an -p tcp | grep LISTEN | grep 864
```

You want to see entries like `100.x.y.z.8642` listening, not `*.8642` or `0.0.0.0.8642`. If you see the latter, the port is exposed to your LAN — fix immediately by recreating the container with the correct `-p` syntax.

External smoke test from a phone or laptop on the same tailnet:

```bash
curl http://hermes-mini.your-tailnet.ts.net:8642/health   # should respond
```

External smoke test from a device **not** on the tailnet (use cellular data with Tailscale off):

```bash
curl http://<public-ip>:8642/health   # should fail / time out
```

If the second test succeeds, the binding is wrong and you have a public-facing agent. Stop everything until it's fixed.

### 8.5 Tailscale-specific Hermes config

For agents that expose the gateway HTTP API (needed only if you use the dashboard or external HTTP clients), set in their `config.yaml`:

```yaml
# Inside the container, listen on all interfaces — Docker's port binding
# restricts external visibility to the Tailscale IP. Inside the container
# 0.0.0.0 is correct; that's container-internal, not host-internal.
api_server:
  enabled: true
  host: "0.0.0.0"
  port: 8642
  key: "<openssl rand -hex 32>"     # 8+ char shared secret for the API
```

The `host: "0.0.0.0"` here is correct and not a security issue — it's the address inside the container, and the container only has its private network plus whatever the host port-binding exposes. The host binding to `${TAILSCALE_IP}` is what keeps it private.

Even with Tailscale handling network access, **always set an `api_server.key`**. Tailscale is the network boundary; the API key is the application boundary. Defense in depth: if a tailnet device is compromised, the attacker still needs the key. Generate one per agent (`openssl rand -hex 32`) and store it in each agent's `.env`.

### 8.6 Tailscale-specific risks worth knowing

- **Tailscale IP can change.** It's stable per device unless you remove/rejoin the tailnet, but if it does change, your bindings break. MagicDNS hostnames are more stable than IPs. Worst case, regenerate `~/hermes/.env` and `docker compose up -d` to recreate.
- **Subnet routing exposes more than you might think.** If you ever enable `tailscale up --advertise-routes=...` on one of these Macs, you're exposing the local subnet to the tailnet. Keep route advertising off unless you specifically need it.
- **ACLs are off by default.** Tailscale's default ACL is "all tailnet members can reach all tailnet ports." For a personal tailnet that's fine; for a shared tailnet (work, family), tighten ACLs in the admin console so only specific devices can reach the agents.
- **Exit nodes affect outbound traffic.** If either Mac is set as an exit node and you route phone traffic through it, that's fine for browsing but unrelated to agent access. Worth being aware of.

---

## 9. Memory architecture

Memory is the *defining* feature of Hermes — the whole reason for this setup rather than six chatbot wrappers. It's worth treating as a first-class part of the design, not an afterthought.

### 9.1 The three layers

Hermes memory has three layers that complement each other. Knowing which layer does what is the key to using them well.

**Layer 1 — Built-in memory (always on, per-agent).**

Two agent-curated markdown files under each agent's `~/.hermes-<name>/memories/`:

- `MEMORY.md` — facts about the world, projects, environment. Hard cap ~2,200 chars by default.
- `USER.md` — facts about you from this agent's perspective. Hard cap ~1,375 chars by default.

Injected into the system prompt as a frozen snapshot at session start. The agent manages them via a `memory` tool — it adds, replaces, and consolidates entries on its own. When a file fills up, the agent merges overlapping entries to make room. Strict character limits keep the system prompt bounded.

**Layer 2 — Session search (always on, per-agent, local).**

Every CLI and messaging session is stored in SQLite at `~/.hermes-<name>/state.db` with FTS5 full-text search. The agent has a `session_search` tool — when you ask "did we discuss X last week?" it queries past sessions and uses an auxiliary LLM (Gemini Flash by default) to summarize matches.

The distinction that matters: **Layer 1 is for facts that should *always* be in the prompt. Layer 2 is for facts that *might* be relevant if you ask.** Different jobs.

This layer is local to each container and never crosses agent boundaries. `coder` cannot search `writer`'s sessions. That's correct behavior — you don't want them blending — but it means cross-agent recall requires Layer 3.

**Layer 3 — External memory provider (optional, additive).**

Hermes ships with 8 plugins — Honcho, OpenViking, Mem0, Hindsight, Holographic, RetainDB, ByteRover, Supermemory. Only one can be active at a time. They run alongside Layers 1 and 2, never replacing them. They add capabilities the built-in layer doesn't have: automatic fact extraction (no agent intervention needed), semantic search across long history, knowledge-graph reasoning, or — in Honcho's case — running user models with dialectic reasoning.

For a six-agent setup with shared user identity, **Honcho is the right choice**. The reason is its data model.

### 9.2 Why Honcho fits a six-agent setup specifically

Honcho models conversations as **peers exchanging messages**. There's one **user peer** (you) that is **global across all agents in the same workspace**, and one **AI peer per agent**, each with its own identity.

Concretely: all six agents share a single understanding of who you are — your preferences, your timezone, your projects, your communication style. But each agent develops its own independent observations and identity. `coder` builds a model of you as a developer; `writer` builds a model of your editorial voice; `concierge` builds a model of your daily rhythms. They don't bleed into each other.

This solves a problem that built-in memory alone cannot: cross-agent continuity for the parts that should be shared, separation for the parts that should be distinct.

Honcho also runs four background processes that do work the built-in layer cannot:

- **Deriver** reads every message and extracts observations about you ("prefers Python", "deadline-driven on weekdays").
- **Dialectic** answers questions about you on demand at one of five reasoning levels.
- **Summary** compresses long sessions into short and long summaries.
- **Dream** runs every ~8 hours, merges redundant observations, deletes outdated ones, and infers higher-level patterns. This is the key consolidation step that makes long-running memory not turn into noise.

### 9.3 Self-host Honcho on the M4 Mini

Honcho's open-source license is **AGPL-3.0** — fine for personal self-hosting. Honcho Cloud is the alternative, but for a system that learns about you over months, you want the data on your own hardware. The Mini is always on, has spare resources, and is already the natural always-on node in your topology.

The Honcho stack is **five containers**: API, Postgres+pgvector, Redis, deriver worker, and summary/dream workers. The community-maintained quick-start at `elkimek/honcho-self-hosted` packages all of this into a single compose file with sensible defaults and Hermes integration. The upstream `plastic-labs/honcho` repo has a similar example compose.

#### Architecture

```
M4 Mini:
├── OrbStack (Docker)
│   ├── hermes-research        (agent container)
│   ├── hermes-concierge       (agent container)
│   ├── hermes-ops             (agent container)
│   └── honcho-stack/          ← new
│       ├── api               (FastAPI server, port 8000)
│       ├── database          (Postgres + pgvector)
│       ├── redis             (cache)
│       ├── deriver           (background worker)
│       └── summary           (background worker)
└── Tailscale interface (100.x.y.z)

MacBook Pro:
├── hermes-coder, hermes-writer → connect to Honcho over Tailscale:
│       http://hermes-mini.your-tailnet.ts.net:8000
```

Honcho itself needs an LLM provider for its background work. The cheapest sensible choice is OpenRouter pointed at Gemini Flash or Claude Haiku — Honcho's reasoning calls are short, frequent, and don't need a top-tier model.

#### Bring it up

On the M4 Mini:

```bash
# Create a directory for the Honcho stack
mkdir -p ~/honcho-stack && cd ~/honcho-stack

# Clone the community quick-start (gives you tuned configs for Hermes)
git clone https://github.com/elkimek/honcho-self-hosted.git config
git clone --depth 1 https://github.com/plastic-labs/honcho.git server

# Copy configs into the server repo
cp config/docker-compose.yml server/
cp config/config.toml server/
cp config/env.example server/.env

# Edit .env — set your LLM provider keys (OpenRouter recommended)
nano server/.env
```

Then bring it up:

```bash
cd ~/honcho-stack/server
docker compose up -d
docker compose logs -f api deriver   # confirm clean startup
```

Verify the API is responding:

```bash
curl -s http://localhost:8000/openapi.json | head -1
```

The compose file binds port 8000 to `127.0.0.1` by default — which is correct, because we want it on Tailscale, not the LAN or the internet. Override the port binding to use Tailscale:

```yaml
# In ~/honcho-stack/server/docker-compose.yml, on the api service:
ports:
  - "${TAILSCALE_IP}:8000:8000"
```

Then `docker compose up -d` to apply. Verify with `lsof`:

```bash
sudo lsof -iTCP -sTCP:LISTEN -P -n | grep 8000
# Should show 100.x.y.z.8000, not *.8000
```

#### Resource impact on the Mini

The Honcho stack adds roughly 1.5–2 GB to the Mini's memory footprint:

- API: ~300 MB
- Postgres + pgvector: ~500 MB (grows with data)
- Redis: ~100 MB
- Deriver worker: ~300 MB
- Summary worker: ~300 MB

Combined with three agent containers (6 GB) + OrbStack (1–2 GB) + macOS (~2 GB), the Mini will sit at ~10–12 GB used out of 16 GB. Workable but not loose. Watch `docker stats` during the first week; if pressure shows up, lower one agent's RAM cap (probably `ops` since it's the most stable).

### 9.4 Per-agent memory configuration

Not every agent needs the full memory stack. Match the configuration to what the agent is for.

| Agent | Built-in memory | Session search | Honcho | Notes |
|---|---|---|---|---|
| `research` | Yes | Yes | Yes (peer: `research`) | Tracks topic interests, source preferences, depth |
| `concierge` | Yes | Yes | Yes (peer: `concierge`) | Models daily rhythms, what reminders land |
| `ops` | Yes (tight) | Optional | **No** | Wants determinism, not personalization |
| `coder` | Yes | Yes | Yes (peer: `coder`) | Language preferences, project conventions |
| `writer` | Yes | Yes | Yes (peer: `writer`) | Voice, edits accepted, tone calibration |
| *(spare)* | TBD | TBD | TBD | — |

`ops` is the deliberate exception. The more personalized it gets, the more it drifts from doing exactly what you asked. Keep it lean. In its `config.yaml`:

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: false   # don't build a USER.md
  memory_char_limit: 1500       # tighter than default
  provider: none                # no external provider
```

For all the other agents that *do* use Honcho, each needs a config file telling Hermes where the Honcho server is and what AI peer name to use. Hermes looks for it at `$HERMES_HOME/honcho.json` (which inside the container resolves to `/opt/data/honcho.json` → on the host, `~/.hermes-<name>/honcho.json`).

Create one per agent. Example for `coder`:

```json
{
  "baseUrl": "http://hermes-mini.your-tailnet.ts.net:8000",
  "hosts": {
    "hermes": {
      "enabled": true,
      "aiPeer": "coder",
      "peerName": "your-name",
      "workspace": "hermes",
      "recallMode": "hybrid",
      "writeFrequency": "every_turn",
      "dialecticReasoningLevel": "medium"
    }
  }
}
```

Key fields:

- `baseUrl` — Honcho server on the Mini, reached over Tailscale.
- `aiPeer` — **unique per agent** (`coder`, `writer`, `research`, `concierge`). This is the identity Honcho uses to build per-agent observations.
- `peerName` — your name. **Same value across all agent configs.** This is what makes the user peer shared.
- `workspace` — keep as `hermes` for all six. The workspace is what binds them into one shared environment.
- `recallMode` — `hybrid` is the sensible default: context is auto-injected into the system prompt *and* tools are available so the model can also query on demand. Other options: `context` (auto-inject only), `tools` (tools only).
- `dialecticReasoningLevel` — `minimal`/`low`/`medium`/`high`/`max`. Higher = better reasoning, more tokens, more cost. Start at `medium` and adjust.

Then in each agent's `config.yaml`:

```yaml
memory:
  provider: honcho
```

Restart the agent's container for changes to take effect.

### 9.5 Initial seed: don't make the agent discover everything

Don't make the agent learn everything from scratch over conversation. Seed each agent's `USER.md` and `MEMORY.md` with 10–20 lines of stable, important facts before first use. This prevents the first weeks from being padded with "wait, what was your name again?" patterns.

**USER.md template (adapt per agent):**

```markdown
# User profile

- Name: <your name>
- Timezone: Europe/Istanbul
- Location: Istanbul
- Languages: <primary>, <secondary>
- OS: macOS (M4 Mini for always-on; M2 MacBook Pro for portable)
- Working hours: <typical hours>
- Communication preference: <concise vs detailed>, <when to ask vs when to act>
- Decision style: <how you want trade-offs presented>
```

Per-agent additions:

- **`coder` USER.md:** preferred languages, code style preferences, projects you work on most, "always show diffs", build tools you use, what error patterns you've seen before.
- **`writer` USER.md:** voice references, audience defaults, kinds of pieces you write, what edits you typically accept vs. reject, your preferred draft → revise rhythm.
- **`research` USER.md:** topics of standing interest, source-quality preferences, depth defaults, citation style.
- **`concierge` USER.md:** schedule patterns, reminder preferences, what's worth pinging you about vs. queuing for digest.
- **`ops` USER.md:** *(leave minimal — `ops` shouldn't develop a user model)*.

**MEMORY.md** is for facts about the world and your environment rather than about you. Things that are stable, agent-useful, and don't change daily. The `coder` agent's MEMORY.md might note "primary repo is ~/projects/foo, uses pnpm, deploys via Vercel." The `research` agent's might note "default web search backend is Firecrawl, prefer primary sources over aggregators."

Write these by hand once. The agent will refine and consolidate them over time, and you should review them periodically (see hygiene below).

### 9.6 Memory hygiene — week 1, week 4, month 3

Memory is not set-and-forget. The agent can write noise to its files, lock in early misunderstandings as permanent facts, or accumulate redundant entries. Plan to review.

**Week 1, per agent (15 min each):**

- Open `~/.hermes-<name>/memories/MEMORY.md` and `USER.md` — read every line.
- Delete anything wrong, outdated, or weirdly phrased. Edits stick.
- If memory is near the character limit and the agent is consolidating poorly, raise the limit in `config.yaml`:
  ```yaml
  memory:
    memory_char_limit: 4000
    user_char_limit: 2000
  ```
- Confirm sessions are being persisted: `ls ~/.hermes-<name>/sessions/` should show files.

**Week 4 (30 min total):**

- Cross-agent check: do `coder` and `writer` have an aligned model of who you are at the user-peer level? Ask each, in separate sessions, *"summarize what you know about me."* Honcho-backed responses should overlap substantially on identity, differ on domain-specific observations. If they don't overlap, Honcho's user peer isn't actually shared — most likely cause is inconsistent `peerName` across agent configs.
- Verify Honcho's dream cycle is consolidating. From the Honcho server: `docker compose logs --tail 200 deriver` should show observations being added and (after dream cycles) consolidated.
- For agents with rich session histories, try `session_search` in a fresh conversation: *"what did we decide about X two weeks ago?"* The agent should pull relevant snippets. If it doesn't, FTS5 may not be indexing — check the `state.db` exists and is growing.

**Month 3 (1 hour):**

- Pruning pass: each agent's MEMORY.md and USER.md will have grown things you didn't expect. Read through and remove genuinely stale facts. The agent may have written `User is working on Project Y` and Project Y ended a month ago.
- Skills review: `ls ~/.hermes-<name>/skills/` for each agent. Useful skills get reused; dead skills accumulate. Delete dead ones.
- Honcho representation export: from any agent, ask *"export your user representation"* — the agent can use `honcho_profile` or read the peer card directly. Save these; they're useful to compare across months and to back up.

### 9.7 Backups

Memory is the asset that compounds. Plan backups now, not after six months.

**What to back up:**

- `~/.hermes-*/memories/` on both Macs — agent-curated MEMORY.md and USER.md.
- `~/.hermes-*/skills/` on both Macs — agent-created skills.
- `~/.hermes-*/sessions/` on both Macs — conversation history (FTS5 index lives in `state.db`).
- `~/.hermes-*/honcho.json` configs — provider settings per agent.
- The Honcho Postgres database on the Mini — this holds Honcho's accumulated observations, conclusions, user peer card, AI peer cards.

**Hermes side (both Macs):**

Trivial — these are just directories. Add them to whatever you already use (Time Machine, Restic, rsync to external). Daily for `memories/`, weekly for `sessions/`.

**Honcho side (Mini only):**

Postgres needs a logical dump, not just a file copy. Weekly cron:

```bash
# ~/honcho-stack/backup.sh
cd ~/honcho-stack/server
docker compose exec -T database pg_dump -U honcho honcho \
  > ~/backups/honcho-$(date +%Y%m%d).sql
# Keep last 8 weekly backups
ls -t ~/backups/honcho-*.sql | tail -n +9 | xargs -r rm
```

Schedule it via `crontab -e`:

```cron
0 3 * * 0  /Users/<you>/honcho-stack/backup.sh >> /tmp/honcho-backup.log 2>&1
```

Verify a backup is restorable at least once. Sometime in month 1, do a test restore into a throwaway container and confirm the dump produces a usable database. A backup you've never restored is not a backup.

### 9.8 Failure modes to know about

- **Honcho server down.** Agents configured with `provider: honcho` will degrade — their built-in memory still works (MEMORY.md, USER.md, session search), but Honcho-injected context disappears and `honcho_*` tools fail. Not catastrophic; agents stay usable. Restart with `docker compose up -d` in the honcho-stack directory.
- **Tailscale link drops between MacBook and Mini.** The two MacBook agents lose their Honcho connection while the link is down. Same degradation as above. Restoring Tailscale immediately restores access.
- **Memory file corruption (rare).** If MEMORY.md or USER.md gets into a weird state, the agent might refuse to write to it. Solution: restore from yesterday's backup, or in the worst case, delete the file — the agent will create a fresh one on next session. You lose accumulated memory in that agent but nothing else.
- **Honcho LLM provider quota exhausted.** Deriver/dialectic/summary/dream calls will fail. The Postgres data is intact; just background processing pauses. Top up the provider, restart the deriver/summary workers. No data loss, but observations from the queue-up period may need to be reprocessed.
- **Memory hit character limit and agent is dropping things.** Either raise the limit in `config.yaml`, or accept that the agent is keeping its memory focused on what's most recent and relevant — which is the design. The header tells you when memory is near full.

### 9.9 What this doesn't solve

A few things worth being explicit about:

- **`ops` won't develop personality over time.** That's deliberate. If you want it to, flip its config to use Honcho with its own AI peer. But the value of `ops` is determinism.
- **The sixth agent's memory profile is undefined.** When you decide what the sixth agent is for, choose its memory config then. Don't pre-allocate.
- **You still need to actually talk to the agents.** The system can only learn from interactions that happen. An agent that gets one message per week will have a thin user model regardless of how good the memory stack is.
- **Cross-agent reasoning isn't automatic.** Honcho's user peer is shared, but each AI peer reasons independently. If `coder` learned something that `concierge` should know, you may need to tell `concierge` once. The agents do not auto-broadcast knowledge to each other.

---

## 10. Web search and fetch: TinyFish

Hermes ships with five built-in web backends (Firecrawl, SearXNG, Parallel, Tavily, Exa). TinyFish isn't one of them, but it integrates cleanly via MCP — and for an agent setup, it's a stronger fit than several of the built-in options. This section covers what TinyFish is, why it earns the slot for our six agents, and how to wire it in.

### 10.1 What TinyFish is and what changes for us

TinyFish is purpose-built for agent web access. Four endpoints under one API key:

- **Search** — `api.search.tinyfish.ai`. Structured JSON results, custom Chromium engine, ~488ms P50 latency, rank-stable across calls. **Free.**
- **Fetch** — `api.fetch.tinyfish.ai`. Renders pages in a real browser, strips nav/scripts/cookie banners, returns clean Markdown/HTML/JSON. **Free.** Failed URLs don't count against quota.
- **Browser** — raw CDP sessions for interactive automation. Metered.
- **Agent** — natural-language browser automation ("go to X, extract Y as JSON"). Metered.

For our six agents, Search and Fetch are the relevant pair, and they're both free. Free tier is **5 search queries/minute** and **25 fetches/minute** per API key — generous for personal use, but worth knowing per-key.

Why this matters for us specifically:

- **Cleaner context, fewer tokens.** TinyFish Fetch strips boilerplate before returning content. With six agents talking to an LLM provider, every kilobyte of cookie-banner HTML stripped is real money saved on context tokens.
- **Built for agent patterns, not human eyes.** Search results are structured JSON — title, URL, snippet, position. No HTML parsing required. Rank-stable means repeated calls return the same results in the same order, which makes session reproducibility and `session_search` indexing behave predictably.
- **Live pages, not cached.** Several search backends return stale results. For `research` agent's job in particular, this is a meaningful quality difference.
- **One key handles all six agents.** Sign up once at agent.tinyfish.ai, distribute the key to whichever agents need search. No per-agent provider setup.

Trade-offs to know:

- **Free tier is rate-limited.** 5 searches/minute across one API key. If `research` and `coder` both decide to do a deep dive simultaneously, they'll hit the cap. We mitigate by either using separate keys for high-traffic agents or queuing through one shared key.
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

Edit each agent's host-side env file. On the Mini:

```bash
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes-research/.env
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes-concierge/.env
# ops can skip — it doesn't need web search
```

On the MacBook:

```bash
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes-coder/.env
echo "TINYFISH_API_KEY=tf_..." >> ~/.hermes-writer/.env
```

#### Step 4: Configure the MCP server in each agent's `config.yaml`

The agent reads its config from `/opt/data/config.yaml` inside the container, which is `~/.hermes-<name>/config.yaml` on the host. Add:

```yaml
mcp:
  servers:
    tinyfish:
      url: "https://agent.tinyfish.ai/mcp"
      headers:
        X-API-Key: "${TINYFISH_API_KEY}"
      enabled: true
```

Hermes's environment substitution syntax (`${TINYFISH_API_KEY}`) reads from the `.env` file inside the container's data dir, which is the file we wrote in Step 3.

#### Step 5: Restart the agent and verify

```bash
docker compose restart hermes-research
docker compose logs --tail 50 hermes-research | grep -i "mcp\|tinyfish"
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

- `research`, `concierge`: **Layer 2** (prefer TinyFish, fall back gracefully). These agents need web access to function; outages should degrade, not break.
- `coder`, `writer`: **Layer 1** (TinyFish only). Their web use is opportunistic; if TinyFish is down, asking the user to try again is fine.
- `ops`: web disabled entirely (already in Section 6). Doesn't need search.

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
      - "${TAILSCALE_IP}:8888:8080"
    volumes:
      - ~/.searxng:/etc/searxng:rw
    environment:
      - BASE_URL=http://hermes-mini.your-tailnet.ts.net:8888/
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
echo "SEARXNG_URL=http://hermes-mini.your-tailnet.ts.net:8888" >> ~/.hermes-research/.env
echo "SEARXNG_URL=http://hermes-mini.your-tailnet.ts.net:8888" >> ~/.hermes-concierge/.env
```

Now the agent has TinyFish as primary (via MCP), SearXNG as fallback (via built-in `web_search`). If TinyFish rate-limits or goes down, the agent still has working search.

Resource impact: SearXNG adds ~500 MB to the Mini. With Honcho (5 containers, ~1.5 GB), three agents (6 GB), and SearXNG (~500 MB), the Mini sits at ~10–11 GB used. Tight but still within budget.

### 10.5 Verifying TinyFish is actually being used

Two checks to run after first setup, and periodically afterward.

**Check 1: Tool usage in agent logs.**

```bash
docker compose logs --tail 200 hermes-research | grep -i "tool_use\|tinyfish_search\|web_search"
```

You want to see `tinyfish_search` and `tinyfish_fetch` calls dominating, with `web_search` (the built-in fallback) only firing rarely or never.

**Check 2: TinyFish dashboard.**

Log into `agent.tinyfish.ai`. The dashboard shows queries per day and which key issued them. You should see steady usage from your agents. If usage is zero despite agents doing web work, the MCP wiring isn't picking up — go back to Step 5 of Section 10.2.

**Rate-limit watch.**

If you see `429` errors from TinyFish in logs, the free tier's 5/min cap is being hit. Options:

1. Add a second TinyFish key for high-traffic agents (typically `research`).
2. Upgrade to a paid tier ($13/month for 1,650 credits if you also need the metered Agent/Browser endpoints; the free Search/Fetch limits also increase).
3. Stagger heavy work via cron (probably not worth it for hobby use).

### 10.6 Per-agent web configuration summary

For reference when wiring up each agent:

| Agent | TinyFish MCP | Built-in `web` toolset | Fallback | Notes |
|---|---|---|---|---|
| `research` | Yes (heavy use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — needs resilience |
| `concierge` | Yes (light use) | Enabled (SearXNG) | SearXNG on Mini | Layer 2 — for occasional lookups |
| `ops` | No | Disabled entirely | None | Doesn't need web |
| `coder` | Yes (medium use) | Disabled | None | Layer 1 — docs/Stack Overflow lookups |
| `writer` | Yes (light use) | Disabled | None | Layer 1 — research while drafting |
| *(spare)* | TBD | TBD | TBD | — |

### 10.7 Per-agent skill for TinyFish (optional but recommended)

TinyFish ships an "Agent Skill" — a `SKILL.md` file that teaches the agent when to reach for search vs. fetch vs. agent vs. browser. For Hermes, the skills system is the natural home for this.

```bash
# In each agent that uses TinyFish, install the skill
# (Run from inside the agent's CLI or via skill_manage)
hermes -p <not-applicable-for-docker> skills install use-tinyfish
```

Inside the Docker setup, the equivalent is to drop the skill into the agent's skills directory:

```bash
mkdir -p ~/.hermes-research/skills/use-tinyfish
# Get the SKILL.md from https://github.com/tinyfish-io/tinyfish-cookbook
# Copy it into the skill directory
```

The skill teaches the agent the escalation ladder: search for finding URLs → fetch for reading pages → agent for interactive tasks → browser for raw CDP. With the skill installed, the agent gets clearer guidance on which TinyFish endpoint to use when — better than relying on SOUL.md hints alone.

### 10.8 What to revisit later

- **Paid tier?** Free is enough for personal multi-agent use. Revisit only if rate limits become a recurring friction.
- **TinyFish Agent and Browser endpoints?** Metered, so they cost credits per call. Worth experimenting with for `research` if a workflow needs login-walled pages or multi-step browsing. Start with the free Search + Fetch.
- **Replacing built-in fallback entirely?** Once you've run TinyFish-primary for a month and confirmed reliability, you can drop SearXNG to save the 500 MB. Defensive setup first, optimization later.

---

## 11. OrbStack-specific notes

- **Verify default context.** Run `docker context ls` and confirm OrbStack is the default. If Docker Desktop was ever installed, the context may need switching with `docker context use orbstack`.
- **Memory ceiling.** OrbStack auto-scales its VM, but you can set a hard cap in OrbStack settings if you want to prevent it from eating the whole machine. With six agents totaling 6 GB across two machines, OrbStack itself should stay around 1–2 GB.
- **Service auto-start.** OrbStack starts on login by default. Verify in OrbStack settings → General. If disabled, your agents won't come back after a reboot.
- **launchd vs container restart.** `--restart unless-stopped` handles container crashes, but if the Mac reboots and OrbStack doesn't auto-start, nothing runs. Two redundant safeguards: enable OrbStack auto-start + use `--restart unless-stopped` on every container.

---

## 12. Evaluation plan

Evaluate the system on three independent axes. Build a small personal eval suite and run it weekly for the first month.

### Layer 1 — host and infrastructure health

For each machine, weekly check:

- All expected containers running: `docker ps` shows the right count
- No container memory cap pressure: `docker stats --no-stream` shows usage well under limits
- No crash loops: `docker ps --filter "status=restarting"` returns empty
- Logs clean: `docker logs --tail 200 hermes-<name>` shows no repeated errors
- Gateway reconnection working: kill the Wi-Fi briefly, confirm gateways reconnect

### Layer 2 — per-agent quality

Build a 15–30 task personal eval suite. For each agent, define 3–5 representative tasks with known-good outcomes. Examples:

- **research:** "Find three recent peer-reviewed papers on X, summarize each in 2 sentences, link each." Score on source quality, summary accuracy, citation correctness.
- **concierge:** "What's on my calendar tomorrow morning? Set a reminder for the 9am call." Score on tool selection (calendar vs. web search), action completion.
- **ops:** "Check disk usage on this host. Alert me if any volume is over 80%." Score on correct command, correct interpretation.
- **coder:** "Find the bug in this function and propose a fix." Score on diagnosis accuracy, fix correctness.
- **writer:** "Draft a 200-word product description for X in voice Y." Score on voice match, word count compliance, factual accuracy.

Run the suite, log results, repeat weekly. Patterns will emerge — which agents drift, which improve as their skill base grows, where the SOUL.md needs sharpening.

### Layer 3 — the learning loop (the actual point of Hermes)

This is what differentiates Hermes from other agent frameworks. Track over a month:

- **Skill creation.** How many skills did each agent autonomously create? Inspect `~/.hermes-<name>/skills/` weekly. Are they useful or noise?
- **Skill reuse.** When the same kind of task recurs, does the agent reuse a skill it created earlier, or recreate it from scratch?
- **Memory accumulation.** Check `~/.hermes-<name>/memories/USER.md` and `MEMORY.md` over time. Is the agent building an accurate model of you and the work, or accumulating noise?
- **Cross-session continuity.** Reference something from a prior conversation without re-explaining it. Does the agent pick it up?

The learning loop is the long-tail value. A one-shot benchmark misses the entire point. Plan to evaluate this over weeks, not minutes.

---

## 13. Safety and operational guardrails

Six agents touching the network and filesystem need explicit guardrails.

**Approvals policy.** In each agent's `config.yaml`:

```yaml
approvals:
  mode: smart    # auxiliary LLM judges risk; escalates genuinely dangerous commands
```

Start with `manual` for the first week to see what gets flagged. Switch to `smart` once you trust the patterns. Never use `off` on the always-on agents.

**Iteration budget.** Default is 90 turns per conversation. For `ops` (which should be short and deterministic), lower it:

```yaml
agent:
  max_turns: 30
```

For `research` (which legitimately needs many tool calls for a thorough job), keep the default or raise to 120.

**Website blocklist.** If any agent should never touch internal hosts, list them:

```yaml
security:
  website_blocklist:
    enabled: true
    domains:
      - "*.internal.local"
      - "192.168.*"
      - "10.*"
      - "100.*"           # blocks reaching other tailnet devices via web tools
      - "169.254.*"       # link-local; blocks cloud metadata endpoints
```

The `100.*` entry deserves attention specifically because of Tailscale. Without it, an agent's `web_fetch` could hit other tailnet members (your phone, work laptop, other Macs). The agent isn't supposed to do that, but the network boundary won't stop it — only the blocklist will. Add this entry for every agent unless one has a specific reason to talk to another tailnet device.

**Tirith security scanning** (optional). If you want pre-execution scanning of shell commands:

```yaml
security:
  tirith_enabled: true
  tirith_fail_open: true   # commands run if scanner unavailable; set false for strict mode
```

Requires the `tirith` binary in the container's PATH.

**Secrets redaction.**

```yaml
security:
  redact_secrets: true   # strip API key patterns from logs and tool output
```

### 13.7 Execution sandboxing — the container is your security boundary

Hermes offers four command-execution backends — `local` (no isolation, runs on the host), `docker`, `modal`, and `daytona` (cloud sandboxes). For bare-metal gateway installs the docs push you toward a sandbox backend, because *"for gateway sessions on the local backend, neither the approval system nor container isolation protects the host."* **We need none of those backends, because our architecture already supplies the sandbox.**

Hermes runs *inside* a per-agent container (OrbStack), so when an agent uses `terminal` or `code_execution` on the default `local` backend, the command executes **inside that container — the macOS host is never touched.** As the docs put it, *"the container itself is the security boundary,"* which is exactly why dangerous-command approval is skipped for container backends. This is the sandboxing payoff of the Section 1 decision: the same choice that gives you isolation gives you the execution sandbox for free.

What that means concretely for the six agents:

- **No `modal`/`daytona`, no docker-in-docker.** Those exist to isolate Hermes when it runs directly on a host. OrbStack + container-per-agent already do that job. Leave the execution backend at `local` inside each container — adding a nested sandbox would be redundant overhead.
- **Residual exposure = bind mounts only.** A runaway shell can reach only what is mounted into its container: each agent's own `~/.hermes-<name>` data dir, plus `coder`'s `~/projects` and `~/godot-projects`. Mount nothing an agent doesn't need. **Never** mount the Docker socket or a home directory into an agent container — that would hand the agent a path back to the host.
- **The best sandbox is no shell at all.** Per Section 6.6, `terminal` and `code_execution` are disabled on `research`, `concierge`, `writer`, and `producer` — those four have zero command-execution surface. Only `coder` (terminal + Python) and `ops` (terminal) can run commands, and only inside their own containers.
- **Credential stripping is on by default.** Hermes removes environment variables matching `KEY` / `TOKEN` / `SECRET` / `PASSWORD` / `CREDENTIAL` / `AUTH` from the child processes of `terminal` and `code_execution`, so LLM-generated code cannot exfiltrate your API keys through the environment. Do **not** defeat this by adding secrets to `terminal.env_passthrough` or to a skill's env config — both bypass the filter.
- **Keep `code_execution` off everywhere but `coder`.** Beyond the focus argument in 6.6, there is a known disclosure (issue #7071) in which the code-execution sandbox injects the project root into the child's `PYTHONPATH`, which can leak config secrets and security rules into LLM-run code. Disabling it on every agent except `coder` shrinks that exposure to a single isolated container.
- **`--user $(id -u):$(id -g)` on `coder`.** Already specified in Sections 6 and 7. Files the agent writes into mounted directories are owned by you rather than root, and the in-container process runs unprivileged.
- **Approvals are defense-in-depth, not the boundary.** The container is the boundary; the approval mode (`smart`, above) is the application-layer net on top. Run both, not either — if a tailnet device or an agent is ever compromised, the second layer still has to be cleared.

---

## 14. Upgrade and maintenance

**Routine upgrade (monthly or when a release ships):**

```bash
docker compose pull
docker compose up -d
```

The data directory (`~/.hermes-<name>/`) is untouched. Skills, memories, sessions, config all survive.

**Backup strategy:**

See Section 9.7 for the full memory backup plan. In summary: `~/.hermes-*/memories/`, `~/.hermes-*/sessions/`, and `~/.hermes-*/skills/` go into whatever you already back up (Time Machine, Restic, rsync). The Honcho Postgres on the Mini needs a weekly `pg_dump`. Verify at least one restore works before relying on it.

**Rollback:**

If an upgrade breaks an agent, pin to the previous image tag in `docker-compose.yaml`:

```yaml
image: nousresearch/hermes-agent:v0.X.Y
```

Then `docker compose up -d` reverts that one agent without touching the others.

**Log management:**

Per-agent logs live in `~/.hermes-<name>/logs/`. They grow indefinitely. Add a monthly rotation cron job or set up `logrotate` if disk becomes an issue.

---

## 15. Open questions to revisit in week 2

These are decisions worth deferring until you have real usage data:

- **Should `coder-server` exist?** Originally pitched as a Mini-resident heavy-compute coder. Worth standing up only if `coder` on the laptop hits CPU limits frequently.
- **Should `ops` get Honcho after all?** Section 9 keeps it off because determinism is the goal. After a month, if `ops` feels too generic or repeats explanations you've given before, flip it on with `aiPeer: "ops"`.
- **Local inference?** Currently everything goes to remote API providers. With six agents the bill adds up — at some point a local 7B model for the cheap tasks (ops, concierge title generation) makes sense. Revisit once you have a month of usage data.
- **Sixth agent's role?** Earmarked for `producer`, the game-development scoring agent (Section 16) — but **deferred**. Phase A is research-only (a single cron on the `research` agent). Build `producer` into the slot only when the opportunity backlog outpaces hand-curation. Until then the slot stays open.

---

## 16. Game development workstream — discovery-first

This is the concrete answer to "I want to start PC and mobile game development but have no time for research, PRDs, market research." You are **exploring**, not committed to a specific game. So this workstream is built as a **discovery engine** — agents surface and score opportunities, you pick one, *then* prototype. It is not a "build-this-game" machine, because you don't yet know the game.

Engine direction: **Godot now, Unity later** (16.7). Provider reality: everything here runs on the **two providers you actually have — your MiniMax token plan and OpenAI Codex** — no Anthropic key, no OpenRouter required for this workstream.

### 16.0 Rollout order — research first, build later

Do not stand up the whole pipeline at once. It phases cleanly, and the early phase is nearly free.

**Phase A (now) — research scout only.** Add the cron job in 16.5 to the `research` agent you are *already* deploying in the core plan. **No new container, no sixth bot, no producer.** It delivers a weekly ranked opportunity digest to Telegram; you read and curate by hand. At low volume, eyeballing beats automated scoring. This is the entire game-dev footprint until friction says otherwise.

**Phase B (deferred — when the backlog earns it) — `producer`.** Stand up the sixth agent (16.3) only once raw opportunities pile up faster than you can skim, *or* you want systematic rubric scoring and a persistent backlog. The trigger is friction, not the calendar. Everything below tagged *(Phase B)* is spec'd now so it's ready, but stays unbuilt until then.

**Phase C — `writer` PRD + `coder` Godot.** Only after you pick a candidate at the 16.9 gate.

So at first sight: one cron on an agent you're building anyway. Nothing more.

### 16.1 Shape and the one hard rule

The pipeline surfaces candidates → scores them → you choose → you prototype. Four agents, reusing three you already have plus filling the spare slot.

**The trap, stated plainly:** market research and PRDs are the *cheapest, lowest-risk* part of game development. The real bottlenecks are (1) finding fun — only a playable prototype tells you, (2) the production grind, (3) launch and discovery. A pile of agents generating weekly trend reports and 50-page design docs *feels* like progress but is procrastination in a suit. Left unchecked, this workstream becomes automated busywork.

**The rule that prevents that:** **timebox discovery to 3–4 weeks**, then commit to exactly **one** prototype. Agents surface candidates; only you, prototyping, can feel whether the loop is fun. Discovery is a phase, not a permanent mode. See the decision gate in 16.9.

### 16.2 The pipeline

```
research (Mini, always-on cron)   →   producer (MacBook, you drive)
   opportunity scout                    backlog + scoring rubric
        │                                       │
        │  raw opportunities                    │  top 3 candidates
        ▼  (Honcho workspace)                   ▼
   ───────────────────────────────────────────────────
   you pick ONE  →  writer (PRD, when picked)  →  coder (Godot prototype)
```

The heavy, always-on web work lives in `research` on the Mini — it runs and delivers to Telegram whether or not your laptop is open. Judgment and curation live in `producer` on the MacBook — it scores when you sit down to review. Honcho's shared workspace carries the candidate list and your evolving taste profile across all four agents, so `writer` and `coder` inherit the context when an idea graduates.

### 16.3 The sixth agent: `producer` *(Phase B — deferred)*

> **Deferred.** Do not build this at first sight. Stand it up only when Phase A's research digests outpace hand-curation or you want rubric scoring. Spec kept here so the day you flip it on, it's a copy-paste, not a redesign.

Fills the reserved MacBook slot from Section 2.

| Field | Value |
|---|---|
| Role | Game-dev product lead: holds the idea backlog, scores opportunities, kills weak ones |
| Personality | Skeptical, honest, anti-hype. Scores against the rubric, refuses to inflate. Kills stale ideas without sentiment. |
| Machine | MacBook Pro (spare slot) |
| Resources | 2 GB RAM, 1 CPU |
| Port | `8647` → producer (dashboard `9124` if enabled) |
| Telegram | **Kayra** — `kayra_<you>_bot` (Section 4 flow); full name Kayra Han |

Compose addition (MacBook `~/hermes/docker-compose.yaml`):

```yaml
  hermes-producer:
    image: nousresearch/hermes-agent:latest
    container_name: hermes-producer
    restart: unless-stopped
    command: gateway run
    ports:
      - "${TAILSCALE_IP}:8647:8642"
    volumes:
      - ~/.hermes-producer:/opt/data
    deploy:
      resources:
        limits:
          memory: 2G
          cpus: "1.0"
```

### 16.4 Model assignment (MiniMax + Codex only)

Balanced so the heaviest agent (`coder`) can't rate-limit your ChatGPT subscription. **Phase A uses only the `research` row** — the rest activate as their agents come online in Phase B/C.

| Agent | Role | Model | Provider | Rationale |
|---|---|---|---|---|
| `research` | opportunity scout | `gpt-5.x` (Codex) | `openai-codex` | Long-context web research, citation discipline; included in ChatGPT sub. Weekly cron = bounded volume, won't blow the daily cap. |
| `producer` | backlog + scoring | `MiniMax-M2.7` | `minimax` | Reasoning over candidates is cheap and frequent; keeps the Codex quota free. |
| `writer` | PRD / store copy | `gpt-5.x` (Codex) | `openai-codex` | Voice and long-form drafting; occasional use = low quota draw. |
| `coder` | Godot prototyping | `MiniMax-M2.7` | `minimax` | Heaviest and most variable volume — keep it **off** the ChatGPT sub so it can't rate-limit research/writer. Pay-per-token scales with active dev. Flip to Codex if GDScript quality disappoints. |

- **Auxiliary tasks** (vision, summarization, context compression) for all four → `MiniMax-M2.7-highspeed`. This removes the OpenRouter dependency from 5.5 for the game-dev agents.
- **Fallback chains** cross the two providers: Codex-primary agents fall back to MiniMax, MiniMax-primary agents fall back to Codex. Two providers, mutual safety net, no third credential.

  `coder` (`~/.hermes-coder/config.yaml`):
  ```yaml
  model:
    provider: minimax
    default: MiniMax-M2.7
  fallback_providers:
    - provider: openai-codex
      model: gpt-5.3
  ```

  `producer` (`~/.hermes-producer/config.yaml`):
  ```yaml
  model:
    provider: minimax
    default: MiniMax-M2.7
  fallback_providers:
    - provider: openai-codex
      model: gpt-5.3
  ```

- **Wallet simplification:** because this workstream needs only MiniMax + Codex, you can drop Anthropic and OpenRouter from the *entire* plan if you want — two providers total. Trade-off: you lose Gemini-Flash-cheap aux (MiniMax-highspeed is pricier than Gemini Flash but still cheap) and the OpenRouter cross-provider fallback pool. For a personal setup, acceptable. Decide in week 2.

### 16.5 `research` as opportunity scout (the cron)

Add a scheduled job to the existing `research` agent. It runs Monday mornings, scans, and delivers a ranked raw-opportunity list to the Telegram home channel (`/sethome` in the research bot first, Section 4.9).

```yaml
# ~/.hermes-research/cron/game-scout.yaml
schedule: "0 8 * * 1"        # Mondays 08:00 local
deliver_to: telegram_home
prompt: |
  Run the weekly game-opportunity scout. Use the TinyFish search+fetch tools.
  1. Steam — surface 5 genres/tags with rising wishlist demand AND weak or
     aging competition (the gap). For each: tag, demand signal, why it's a gap.
  2. Mobile — top-grossing plus fastest-climbing titles in 3 casual / casual-mid
     categories. Note what mechanic is driving the climb.
  3. Complaint mining (PRIORITY) — pull top critical reviews of 5 popular games
     in the genres I track; extract concrete "I wish it had X" desires. Player
     unmet-desire is the highest-signal seed, worth more than chart summaries.
  4. Reddit r/gamedev + r/IndieGaming — recurring pain points, asset/tool gaps,
     "why does no game do X" threads.
  Output a ranked list of 5–8 raw opportunities, each as
  {title, genre, signal, the-gap, solo-buildable?}. Write them to the Honcho
  workspace so the producer agent can score them. Keep the Telegram digest
  under 600 words.
```

Why complaint mining is flagged priority: chart-topper summaries tell you what everyone already knows sells (cozy, survival-craft, extraction). Unmet player desire in reviews of *existing* games is where a solo dev's edge actually hides.

### 16.6 `producer` backlog + scoring rubric *(Phase B — deferred)*

> **Until producer exists (Phase A):** the scout's weekly digest lands in Telegram and you curate by hand — star what interests you, ignore the rest. No backlog file, no automated scoring. The rubric below is still worth keeping in your head as you skim. When manual curation gets tedious, that's the signal to build producer and automate it.

`producer` keeps a persistent backlog at `/opt/data/backlog.md` (→ `~/.hermes-producer/backlog.md` on the host, Honcho-shared). It scores each new candidate Mondays, right after the scout runs.

```yaml
# ~/.hermes-producer/cron/score-backlog.yaml
schedule: "0 9 * * 1"        # Mondays 09:00, after the 08:00 scout
deliver_to: telegram_home
prompt: |
  Read this week's raw opportunities from the Honcho workspace and the existing
  backlog at /opt/data/backlog.md. Score each NEW candidate 1–5 on:
    - Buildable : solo prototype in under 3 months?
    - Loop      : core loop expressible in one sentence?
    - Discovery : niche, searchable, streamable — can players find it?
    - Money     : monetization obvious (premium / ads+IAP / DLC)?
  Do NOT score "fun" or taste — that gate belongs to the user alone.
  Append each scored candidate to backlog.md with today's date and the four
  scores. Strike through (kill) anything that has sat unpicked for >6 weeks.
  Deliver the top 3 total-scorers as one-line pitches to Telegram.
```

The rubric, for reference:

| Gate | Question | Scored by |
|---|---|---|
| Buildable | Solo prototype in <3 months? | producer |
| Loop | Core loop in one sentence? | producer |
| Discovery | Niche, searchable, streamable? | producer |
| Money | Monetization obvious? | producer |
| **Taste** | Do *you* want to build it for 6 months? | **you — never the agent** |

The taste gate is deliberately outside the agent's reach. A high-scoring candidate you have no desire to build for half a year is a trap; a mid-scoring one you're itching to make is the right call. The agents narrow the field; you make the call.

### 16.7 `coder`: Godot-first

**Engine verdict for the exploration phase: Godot.** Free, no royalties, lightweight, and — critically for testing many throwaway prototypes — *fast iteration*. Excellent 2D, solid mobile export, and GDScript is very LLM-friendly so the `coder` agent writes it well. Cheap to build a prototype and cheaper to throw it away.

**Unity is a commit trigger, not the default.** Switch a *chosen* game to Unity only when it needs mobile-monetization SDK depth (ads/IAP/analytics — LevelPlay, AppLovin, Firebase) or real 3D. Heavier, slower loop. Don't pay that tax while still exploring.

Add a Godot projects volume to the `coder` service (MacBook compose):

```yaml
    volumes:
      - ~/.hermes-coder:/opt/data
      - ~/projects:/workspace/projects
      - ~/godot-projects:/workspace/godot     # add this
```

The Godot *engine* runs on the host (you open the editor, play-test, export builds); the `coder` agent edits scripts and scenes in the mounted directory and can run `godot --headless` for quick checks. `--user $(id -u):$(id -g)` (already set for coder in Section 6) keeps created files owned by you, not root.

### 16.8 SOUL.md seeds

**`producer` SOUL.md:**

```
You are a skeptical game-dev product lead for a solo developer who is still
exploring what to build. Your job is to keep an honest opportunity backlog,
score candidates against a fixed rubric (buildable, loop clarity, discovery,
monetization), and kill weak or stale ideas without sentiment. Never inflate a
score to be encouraging. Never score "fun" or taste — that is the developer's
call alone; surface the trade-offs and stop there. Bias toward small,
solo-buildable scope. When you flag a top candidate, give a one-sentence pitch
and the single biggest risk. Brevity over enthusiasm.
```

**`research` SOUL.md addendum** (append to the existing research personality):

```
For the weekly game-opportunity scout, weight concrete unmet player desire
(mined from reviews of existing games) above generic trend summaries. A gap a
solo dev can fill beats a hot genre a solo dev can't compete in.
```

**`writer` SOUL.md addendum:**

```
When drafting a game PRD or design doc, keep it lean — two pages, not fifty.
Lead with the one-sentence core loop, the target player, and the single thing
that makes it worth building. A short doc that ships beats a long doc that
becomes the project.
```

### 16.9 Decision gate

The discovery phase ends on a date, not on a feeling.

- **Timebox:** 3–4 weeks of scout + score cycles. Set the end date when you stand up `producer`.
- **Graduate to prototype** when a candidate clears the rubric *and* the taste gate — i.e. it scores well *and* you want to build it for six months. Then `writer` drafts the lean PRD and `coder` starts the Godot prototype.
- **Kill criteria:** `producer` strikes any candidate unpicked after 6 weeks. If the *whole backlog* goes stale with nothing you want to build, that's signal too — widen the scout's genres or accept that exploration via agents has hit its limit and you need to prototype something rough to find your own taste.
- **Hard stop:** at the end of the timebox, pick the best available candidate and prototype it even if it's imperfect. A flawed prototype teaches more than a fifth week of scoring.

### 16.10 What this doesn't solve

- **Fun is unprovable on paper.** No scout, no rubric, no PRD finds it. Only a playable prototype does. The entire pipeline exists to get you *to* a prototype faster, not to replace it.
- **Taste is yours.** The agents narrow the field honestly; the choice of what's worth six months of your life is not delegable.
- **Market data is shallow by default.** Agents surface the legible signal. Your edge is the illegible insight — the niche you understand that the charts don't show. Treat scout output as a starting point, not an answer.
- **Docs over-produce if unchecked.** Re-read 16.1's trap monthly. If you have more design docs than playable builds, the workstream has inverted and needs correcting.

---

## Summary

Seven independent Docker containers, two machines, OrbStack underneath, Tailscale as the network boundary, seven dedicated Telegram bots (Turkic-named — Kam, Mergen, Umay, Asena, Ülgen, Korkut, Kayra) as the human interface, self-hosted Honcho on the Mini as the shared memory backbone, TinyFish via MCP as the web search and fetch layer. Three model providers: Codex OAuth (ChatGPT subscription) for the conversational agents `research` and `writer`; Anthropic API key for the coding agent `coder`; MiniMax API key for `concierge` and `ops`. One OpenRouter key across all six for cheap auxiliary tasks (Gemini Flash) and cross-provider fallbacks. No `hermes profile` commands. Each agent has its own host directory, port, bot token, personality, and skill set; memory is three layers — built-in MEMORY.md/USER.md per agent, local FTS5 session search per agent, and Honcho on top for shared user modeling with separate AI peers; web access is TinyFish-primary with SearXNG as local fallback for agents that need resilience. All ports bound to the Tailscale interface, nothing exposed to LAN or internet. Build in three phases — one agent end-to-end, prove isolation with the second, scale to six. Evaluate weekly on infrastructure health, per-agent task quality, and the learning loop that makes Hermes distinct.

The non-negotiable disciplines: Section 4's six-bot prep done before any container starts, Phase 2's filesystem isolation test (Section 6), Section 8's bind-verification check (`lsof` shows Tailscale IP, not `0.0.0.0`), Section 9.6's week-1 memory review, Section 9.7's first verified Honcho backup restore, and Section 10.5's check that TinyFish is actually being called rather than the built-in fallback. Each takes under an hour. Each is what separates a setup that works on day one from one that still works on day three hundred.
