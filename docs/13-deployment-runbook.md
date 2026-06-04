# Deployment Runbook

[← All docs](../README.md)

---

The concern docs (01–12) explain *why*; this is the *what, in order*. Follow top-to-bottom.

> ⚠️ = a **verify-gate**: the plan makes an assumption about native Hermes that hasn't been tested against a live install. Confirm it before trusting it, and adjust the runbook if reality differs.

```mermaid
flowchart LR
    s0["0 · keys"]:::p --> s1["1 · install<br/>+ verify CLI ⚠️"]:::v --> s2["2 · 7 bots"]:::p
    s2 --> s3["3 · Doruk<br/>soak 24h"]:::a --> s4["4 · Derya<br/>coexist test"]:::a --> s5["5 · scout cron"]:::a
    s5 --> s6["6 · Phase B<br/>Honcho · coder · writer"]:::b --> s7["7 · Phase C<br/>game-dev"]:::c
    classDef p fill:#1E88E5,stroke:#0D47A1,color:#fff
    classDef v fill:#E53935,stroke:#B71C1C,color:#fff
    classDef a fill:#43A047,stroke:#1B5E20,color:#fff
    classDef b fill:#8E24AA,stroke:#4A148C,color:#fff
    classDef c fill:#FB8C00,stroke:#E65100,color:#fff
```

## Step 0 — Accounts & keys (before anything)

- [ ] **MiniMax $20 Token Plan** — platform.minimax.io. ⚠️ Confirm **M3 is included at the $20 tier**; grab the **API key + base URL** (token plans can use a distinct endpoint — [docs/04 §5.2](04-models.md)).
- [ ] **OpenRouter key** — openrouter.ai, ~$5–10 credit (aux + fallback + overflow — §5.5).
- [ ] **Codex creds** — existing `~/.codex/auth.json` (ChatGPT Desktop / Codex CLI) or be ready for device-code login (coder + writer — §5.3). ⚠️ Accepted-risk — read §5.0 first.
- [ ] **TinyFish key** — web research ([docs/08](08-web-search.md)).
- [ ] **Mac Mini M4** — macOS current; **auto-login enabled** so launchd agents survive reboot ([docs/01 §11](01-architecture.md)); Docker/OrbStack installed (for Honcho + SearXNG **only**).

## Step 1 — Install Hermes + verify the CLI ⚠️

- [ ] Install **native** (Homebrew formula / official installer — [docs/10 §14](10-operations.md)). `hermes --version`.
- [ ] ⚠️ **Confirm the CLI verbs.** The plan assumes `hermes profile create <slug>`, `hermes setup --profile <slug>`, `hermes gateway run --profile <slug>`, `hermes tools --list`. Run `hermes --help` / `hermes profile --help` / `hermes gateway --help` and adjust if they differ.
- [ ] ⚠️ **Supervision story.** Check whether native Hermes ships its own multi-profile runner or you wire **launchd LaunchAgents** yourself ([docs/05 §7](05-deployment.md)). Decide before Phase A.

## Step 2 — Telegram bots ×7

- [ ] @BotFather → `/newbot` ×7. Slug-based usernames `general_<you>_bot … producer_<you>_bot` ([docs/03](03-telegram-bots.md)). Save tokens.
- [ ] `cp scripts/bot-tokens.env.example scripts/bot-tokens.env && chmod 600 scripts/bot-tokens.env` → fill `ALLOWED_USERS` (@userinfobot) + the 7 tokens.
- [ ] `./scripts/setup-bots.sh` → sets bot profiles + writes `~/.hermes/<slug>/.env`. Watch for `getMe` **WARNING** lines (bad/revoked tokens).
- [ ] Per bot in BotFather: `/setprivacy` Disable, `/setjoingroups` Disable.

## Step 3 — Phase A: research (Doruk) end-to-end

- [ ] `hermes profile create research` → `hermes setup --profile research`.
- [ ] `config.yaml`: `provider: minimax` / `default: MiniMax-M3` (§5.2). ⚠️ Use the **verified** model string + base URL from Step 0.
- [ ] Add OpenRouter aux (§5.5) + fallback chain (§5.7).
- [ ] Write **Doruk** SOUL.md ([docs/02 §6.7](02-agents.md)); prune toolsets ([docs/05 §6.6](05-deployment.md)).
- [ ] launchd LaunchAgent + load. Message the bot → it answers; a session file appears under `~/.hermes/research/sessions/`.
- [ ] **24 h soak** — healthy next morning, RAM in budget (Activity Monitor). Acceptance = docs/05 Phase 1.

## Step 4 — Phase A: general (Derya) + isolation tests

- [ ] Same pattern, profile `general` — Derya SOUL, MiniMax M3, Codex fallback.
- [ ] **Per-profile state test** (docs/05 Phase 2): a memory note in Doruk is **not** visible to Derya.
- [ ] **Independent-lifecycle test**: `launchctl kickstart -k` research → Derya keeps running.

## Step 5 — Game-scout cron

- [ ] Add `~/.hermes/research/cron/game-scout.yaml` ([docs/11 §16.5](11-game-dev.md)); `/sethome` in the research bot. Confirm the Monday digest lands in Telegram.

## Step 6 — Phase B (only once Phase A is proven)

- [ ] **Honcho** — 5-container stack via Docker, bound `127.0.0.1:8000` ([docs/07 §9.3](07-memory.md)). ⚠️ Validate `honcho.json` peer config (peers = **slugs**) + agents reach it over loopback.
- [ ] **SearXNG** — Docker, `127.0.0.1:8888` ([docs/08 §10.4](08-web-search.md)).
- [ ] **TinyFish MCP** — wire + key (docs/08).
- [ ] Add profiles: **Tuna** (concierge, MiniMax) · **Naz** (coder — **Codex**, `terminal`+`code_execution`, Godot projects, fenced per [docs/09 §13.7](09-security.md)) · **Ozan** (writer — Codex). ⚠️ **coder is the ToS + shared-ChatGPT-quota watch-point** (§5.3); A/B it vs M3 / DeepSeek V4 Pro (§5.12).
- [ ] Wire Honcho peers on the agents that use it (docs/07).

## Step 7 — Phase C: game-dev

- [ ] A picked idea (16.9 gate) → **Ozan** lean PRD → **Naz** Godot prototype, native on the Mini's **Metal GPU** ([docs/11](11-game-dev.md)).

## Deferred — don't build at first

- **ops (Nilay)** — only once there's a fleet to watch, **and** after deciding safe host-visibility (host-metrics mount, never the Docker socket — [docs/10 open-Q](10-operations.md)).
- **producer (Sarp)** — when the game-idea backlog outpaces hand-curation ([docs/11 §16.3](11-game-dev.md)).
- **kanban** — off until recurring auto-handoffs are real ([docs/12 §17.3](12-agent-comms.md)).

## Optional / later

- **Tailscale** — only if you enable the dashboard or HTTP API; Telegram needs none ([docs/06](06-networking.md)).
- **DeepSeek V4 Pro for coder** — the clean-API-key de-risk off Codex (§5.12).
- **Personal (non-studio) agents** — finance, fitness, etc., one profile per domain ([docs/02 §2.2](02-agents.md)).

---

**Critical path to a first running fleet:** Step 0 → 1 → 2 → 3. Everything after Step 5 is incremental. The riskiest unknowns are the ⚠️ gates in Steps 0–1 (native CLI, launchd, token-plan endpoint) — resolve those against the live install and the rest is mechanical.
