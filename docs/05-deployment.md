# Deployment — Dirs, Profiles, Phases, Toolsets, Supervision

[← All docs](../README.md)

---

```mermaid
flowchart LR
    P1["Phase 1 · day 1<br/>native install + first profile<br/>researcher · 24h soak"]:::p1 --> P2["Phase 2 · day 2-3<br/>second profile<br/>prove per-profile state + launchd"]:::p2
    P2 --> P3["Phase 3 · week 1+<br/>remaining profiles<br/>copy the pattern"]:::p3
    classDef p1 fill:#1976D2,stroke:#0D47A1,color:#fff
    classDef p2 fill:#388E3C,stroke:#1B5E20,color:#fff
    classDef p3 fill:#7B1FA2,stroke:#4A148C,color:#fff
```

Everything runs **native** on one Mac Mini M4 — one Hermes install, one profile per agent under `~/.hermes/`. No agent runs in a container. Docker is used **only** for Honcho and SearXNG (Section 7).

## 3. Directory, profile, and port conventions

A single install keeps all profiles under `~/.hermes/`. Each agent is a profile; its slug is the profile name.

```
~/.hermes/                 ← default profile (unused by the fleet)
├── kanban.db              ← shared board (created only if/when kanban is enabled — Section 17)
└── profiles/
    ├── researcher/    ← Doruk
    ├── general/       ← Derya
    ├── assistant/     ← Tuna
    ├── marketing/     ← Nilay
    ├── coder/         ← Naz
    ├── writer/        ← Ozan
    └── producer/      ← Sarp
```

*(Verified against v0.16.0: named profiles live under `~/.hermes/profiles/<slug>/`; per-profile gateway logs at `~/.hermes/profiles/<slug>/logs/gateway.log`. `profile create` also drops a wrapper at `~/.local/bin/<slug>`, so `researcher setup` ≡ `hermes -p researcher setup`.)*

Each profile directory holds the full state for that agent:

| Path | Contents |
|---|---|
| `.env` | API keys and bot token |
| `config.yaml` | Hermes configuration |
| `SOUL.md` | Agent personality and instructions |
| `sessions/` | Conversation history |
| `memories/` | Persistent memory store (USER.md, MEMORY.md) |
| `skills/` | Agent-created and bundled skills |
| `cron/` | Scheduled job definitions |

**Ports.** Each gateway reaches Telegram **outbound** (long-poll), so it needs **no inbound port** to function. A host port is required only if you turn on a per-profile feature that listens:

- the **HTTP API server** (`api_server`) — not needed for Telegram or for agent-to-agent, which is local now ([Section 17](12-agent-comms.md)); enable only for the dashboard or external HTTP clients
- the **dashboard** — one port per profile if you enable it (`9119+`)

If you do expose any port, bind it to the Tailscale interface only and key it — see [Section 8](06-networking.md). Most profiles expose nothing.

---

## 6. Deployment plan — phased

Do not stand up all agents at once. Build in three phases so problems get isolated as they arise.

**Prerequisites:** Section 4 done (bots exist, tokens saved, your Telegram user ID in hand) and Section 5 done (API keys from MiniMax, OpenRouter, and Codex credentials available). Keys and tokens get pasted during per-profile setup.

### Phase 1 — native install + first profile (day 1)

Pick one agent (recommend `researcher` — single platform, no project mounts) and get it fully working before touching anything else.

**Step 1.1: Install Hermes natively**

Install via the official installer / Homebrew formula (exact command in [Section 14](10-operations.md)). Confirm:

```bash
hermes --version
```

All state will live under `~/.hermes/`.

**Step 1.2: Create the profile and run setup**

```bash
hermes profile create researcher
hermes -p researcher setup        # verified v0.16.0: global -p/--profile flag, not `setup --profile`
                                # (or use the wrapper: `researcher setup`)
```

Configure: the model provider (`researcher` uses MiniMax M3 per Section 5.2), the relevant API keys, and the Telegram bot token. The wizard writes to `~/.hermes/profiles/researcher/.env`.

**Step 1.3: Edit the SOUL.md**

```bash
nano ~/.hermes/profiles/researcher/SOUL.md
```

Write the personality (the full SOUL is in Section 6.7; an example early draft):

```
You are a methodical research assistant. Find primary sources, synthesize them
faithfully, and surface what you don't know. Cite every non-trivial claim with
its URL. Prefer two reliable sources over one. When evidence is mixed, say so —
do not flatten disagreement. Concise by default; expand only when asked.
```

**Step 1.4: Run the gateway**

```bash
hermes -p researcher gateway run   # foreground; verified v0.16.0
```

Confirm it connects and responds to a Telegram message. Once it works in the foreground, install the built-in launchd service so it survives logout/reboot: `hermes -p researcher gateway install` (Section 7).

**Step 1.5: Verify**

```bash
hermes profile list                                    # researcher is present
tail -f ~/.hermes/profiles/researcher/logs/gateway.log       # no errors at startup
```

Send the bot a message; confirm a new session file appears under `~/.hermes/profiles/researcher/sessions/`. Leave it running overnight via launchd and check it's healthy in the morning.

**Acceptance criteria for Phase 1:**
- Gateway stays up for 24+ hours (under launchd) without crashing
- A skill gets created and persists across a gateway restart
- A new session picks up context from a memory file written in an earlier session

### Phase 2 — second profile, validate coexistence (day 2–3)

Stand up `general` (Derya). The point of this phase is to prove **two profiles run side-by-side with separate state**, supervised independently.

**Step 2.1: Replicate the pattern**

```bash
hermes profile create general
hermes -p general setup          # different bot token, different SOUL.md
```

**Step 2.2: Supervise it too**

`hermes -p general gateway install` (Section 7). Both gateways now run.

**Step 2.3: Per-profile state test** *(replaces the old container marker test)*

From a chat with `researcher`, have it write a memory note. From a chat with `general`, ask it to recall that note — it **must not** have it. Built-in session/memory search never crosses profiles (`general` cannot see `researcher`'s sessions). Confirm each profile has its own `~/.hermes/profiles/<profile>/sessions/` and `memories/`.

> **Note:** This is *state* separation, not a *filesystem* sandbox. A shell-capable profile could still read another profile's files on disk — that's why **only `coder` gets a shell**, and why `coder` is fenced ([Section 13](09-security.md)). Cross-profile *sharing* of the things that should be shared (your user model) is Honcho's job ([Section 9](07-memory.md)).

**Step 2.4: Independent-lifecycle test**

`launchctl kickstart -k` the `researcher` agent (force-restart). Confirm `general` keeps running and responding throughout. launchd supervises each profile independently — one bouncing doesn't disturb the other.

**Acceptance criteria for Phase 2:**
- Both gateways run simultaneously under launchd without conflict
- Per-profile state separation confirmed (recall test)
- Independent lifecycle confirmed (restart test)
- Different bot tokens, different personalities, both reachable

### Phase 3 — remaining profiles (week 1+)

Once Phase 2 passes, the rest are mechanical. Copy the pattern, change names, SOUL.md, toolsets.

For each new agent:
1. `hermes profile create <name>`
2. `hermes -p <name> setup` (unique bot token, model, keys)
3. Write the SOUL.md
4. Prune toolsets in `config.yaml` (Section 6.6)
5. `hermes -p <name> gateway install`

**Special considerations per agent:**

- **`coder`** — the only agent that runs code, and it runs **natively** for Metal GPU + the Godot GUI ([Section 16](11-game-dev.md)). Mount nothing; it already has your user's filesystem. Fence it: `approvals: smart`, credential redaction, website blocklist, optional `docker` code-exec backend ([Section 13](09-security.md)). Point it at your projects directory in config (e.g. `~/godot-projects`).
- **`marketing`** — no shell; `web` + TinyFish for market/competitor research, `cronjob` for the devlog/social cadence. Briefs `writer` when it needs finished copy.
- **`writer`** — if you want finished drafts in a tidy spot, set its file output to `~/Documents/writer-output/` in config.

### 6.6 Toolset hygiene — disable what each agent doesn't need

Hermes loads **~17 toolsets by default**, and each enabled toolset injects its schemas into *every* prompt that agent sends. Pruning is the single cheapest lever on three things at once: **token cost** (fewer schemas per prompt), **risk surface** (`terminal`, `code_execution`, `browser` = shell, arbitrary code, full browser automation), and **focus** (fewer tempting wrong tools). With no container boundary, **toolset pruning is now a primary security control**, not just a cost lever — a no-shell agent simply cannot run host commands.

First, separate three things people conflate:

| Thing | What it is | Token cost | What to do |
|---|---|---|---|
| **Toolsets** | Capabilities injected into the system prompt | **High** — schemas sit in every prompt | **Prune per agent (this section)** |
| **Skills** | On-demand knowledge docs (progressive disclosure) | ~0 until the agent opens one | Don't prune; only *add* useful ones (e.g. the TinyFish skill) |
| **Plugins** | Memory / context / model / platform providers | Pick one of each | You pick **Honcho** as the memory provider (Section 9); the rest auto-load |

**The high-value disables** (all default-on, rarely needed):

- **`browser`** — heavy (≈10 tools, needs Chromium). We use **TinyFish via MCP** (Section 10) for web. Disable everywhere; re-enable only on an agent that genuinely needs interactive page automation (none do, at first).
- **`terminal`** — shell access. Native, this shell is your **real Mac**, not a container. **Only `coder` gets it.** Everyone else: off.
- **`code_execution`** — runs Python on the host. Only `coder`.
- **`image_gen`** — requires a FAL.ai key you don't have; dead schema weight. Disable everywhere.
- **`delegation`** — spawns in-process subagents = surprise token spend. Disable until you deliberately want fan-out.

**Per-agent matrix** (core toolsets — `memory`, `session_search`, `skills`, `clarify`, `safe` — always kept and omitted):

| Toolset | researcher | assistant | marketing | coder | writer |
|---|:--:|:--:|:--:|:--:|:--:|
| `terminal` | ✗ | ✗ | ✗ | ✓ | ✗ |
| `code_execution` | ✗ | ✗ | ✗ | ✓ | ✗ |
| `browser` | ✗ | ✗ | ✗ | ✗ | ✗ |
| `web` (built-in) | ✓ fallback | ✓ fallback | ✓ fallback | ✓ fallback | ✓ fallback |
| `file` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `vision` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `image_gen` | ✗ | ✗ | ✗ | ✗ | ✗ |
| `tts` | ✗ | ✓ | ✗ | ✗ | ✗ |
| `cronjob` | ✓ | ✓ | ✓ | ✗ | ✗ |
| `messaging` | ✗ | ✗ | ✗ | ✗ | ✗ |
| `delegation` | ✗ | ✗ | ✗ | opt | ✗ |
| `kanban` | opt | ✗ | ✗ | opt | opt |
| `todo` | ✗ | ✗ | ✗ | ✓ | ✗ |

The `kanban` row is **opt** on the studio pipeline (`researcher`, `writer`, plus `producer`/`coder`) — off at first, flipped on only if the backlog outgrows hand-curation ([Section 17](12-agent-comms.md)). `marketing` mirrors `researcher` (web + TinyFish + cron, no shell). Everything else is off where unused.

**Per-agent `disabled_toolsets`** (paste into each `config.yaml`):

```yaml
# general (Derya)  — conversational; keeps web/vision/tts/cronjob
agent:
  disabled_toolsets: [terminal, code_execution, browser, image_gen, delegation, messaging, todo, kanban]

# researcher
agent:
  disabled_toolsets: [terminal, code_execution, browser, image_gen, tts, delegation, messaging, todo]

# assistant
agent:
  disabled_toolsets: [terminal, code_execution, browser, image_gen, delegation, messaging, todo, kanban]

# marketing (Nilay) — web + TinyFish + cron, no shell; mirrors researcher
agent:
  disabled_toolsets: [terminal, code_execution, browser, image_gen, tts, delegation, messaging, todo]

# coder  (the only agent with shell + code execution; web kept on for SearXNG fallback)
agent:
  disabled_toolsets: [browser, image_gen, tts, cronjob, messaging, delegation]

# writer  (web kept on for SearXNG fallback)
agent:
  disabled_toolsets: [terminal, code_execution, browser, image_gen, cronjob, messaging, delegation, todo]
```

Opt-in toolsets (`search`, `video`, `video_gen`, `moa`, `debugging`, `computer_use`, `homeassistant`, `spotify`, `discord`, `feishu_doc`, `feishu_drive`, `yuanbao`, `x_search`) are **already off** — do not list them. Verify the live result per agent:

```bash
hermes -p <name> tools list   # active vs available-but-disabled
```

**`producer` (Phase B):** disable `[terminal, code_execution, browser, image_gen, vision, tts, delegation, messaging]`. Keeps `web` (SearXNG fallback — every agent is Layer 2, [docs/08 §10.6](08-web-search.md)), `file` (writes `backlog.md`), `cronjob` (weekly scoring), optional `kanban`, plus the core set.

**One flag worth remembering:** native, **`terminal` is the real host.** `coder`'s shell sees your actual Mac — your files, your other profiles' `.env`. There is no container wall. That is the whole reason **only `coder` gets a shell** and it is fenced in [Section 13](09-security.md).

**Phase A relevance:** only `researcher` (and then `Derya`/`general`) is live at first, so their disable lists are all you need on day one. The rest apply as each agent comes online.

---

## 7. Supervision — launchd, via the built-in installer (one service per profile)

The s6 supervision tree only exists inside the Docker image. Native, **launchd** is the supervisor: it starts each gateway at login and restarts it on crash — the native equivalent of `--restart unless-stopped`.

**Verified against v0.16.0: do NOT hand-roll plists.** Hermes ships a per-profile launchd installer. It generates a plist labeled `ai.hermes.gateway-<profile>` with `RunAtLoad` + `KeepAlive`, a sane `PATH`, `HERMES_HOME` pinned to the profile dir, and logs at `~/.hermes/profiles/<profile>/logs/gateway.log` (+ `gateway.error.log`):

```bash
hermes -p researcher gateway install    # write + bootstrap the launchd service
hermes -p researcher gateway status     # service installed/running?
hermes -p researcher gateway restart    # bounce one profile
hermes gateway list                   # whole fleet at a glance
```

For surgical control (the watchdog, the independent-lifecycle test) the launchd label is `ai.hermes.gateway-<profile>`:

```bash
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway-researcher   # force-restart one agent
launchctl bootout   gui/$(id -u)/ai.hermes.gateway-researcher      # stop + disable
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.gateway-researcher.plist  # re-enable
```

**Two redundant safeguards** so the fleet survives a reboot:
- LaunchAgents load at **login** — so enable **automatic login** on the Mini (System Settings → Users & Groups), or nothing starts after an unattended reboot.
- `KeepAlive` handles process crashes; auto-login handles machine reboots. Run both.

### Docker — services only

The two stateful **services** stay containerized (far easier than native Postgres/SearXNG). Nothing else uses Docker.

- **Honcho** is a **five-container stack** (API, Postgres+pgvector, Redis, deriver, dream workers) — use the compose from [Section 9.3](07-memory.md), bound to `127.0.0.1:8000`. Do not hand-roll a single-container `honcho` here; follow docs/07.
- **SearXNG** is one container — see [Section 10.4](08-web-search.md). Bind it to `127.0.0.1:8888:8080` (the `8888` host port the agents are configured against in docs/08).

Both bind to `127.0.0.1` — the native agents reach them over loopback; nothing is exposed off-box. Start them with `docker compose up -d` from their compose directory; upgrade with `docker compose pull && docker compose up -d`. Agent upgrades are native (Section 14), not Docker.

```bash
docker compose up -d            # start Honcho + SearXNG
docker compose pull && docker compose up -d   # upgrade the services
```

Agent upgrades are native, not Docker — see [Section 14](10-operations.md).

---
