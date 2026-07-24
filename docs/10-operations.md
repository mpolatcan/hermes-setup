# Operations — Eval, Maintenance, Open Questions

[← All docs](../README.md)

---

## 12. Evaluation plan

Evaluate the system on three independent axes. Build a small personal eval suite and run it weekly for the first month.

### Layer 1 — host and infrastructure health

On the Mini, weekly check:

- All expected gateways running: `hermes profile list` + `launchctl list | grep ai.hermes` show the right count
- No memory pressure: `vmm_stat` / Activity Monitor shows the Mini well under 16 GB; no runaway profile (native has no per-agent cap — Section 1.1)
- No crash loops: `launchctl print gui/$(id -u)/ai.hermes.gateway-<profile>` shows a stable PID and no climbing failure count (`launchctl list` shows the current PID/exit code, not a restart history)
- Logs clean: `tail -n 200 ~/.hermes/logs/gateways/<name>/current` shows no repeated errors
- Services up: `docker compose ps` shows Honcho + SearXNG running
- External planes reachable: `ntn whoami` resolves the expected Notion workspace and `hermes -p <profile> secrets onepassword status` reports healthy mappings without printing values
- Gateway reconnection working: kill Wi-Fi briefly, confirm gateways reconnect

### Layer 2 — per-agent quality

Build a 15–30 task personal eval suite. For each agent, define 3–5 representative tasks with known-good outcomes. Examples:

- **researcher:** "Find three recent peer-reviewed papers on X, summarize each in 2 sentences, link each." Score on source quality, summary accuracy, citation correctness.
- **assistant:** "What's on my calendar tomorrow morning? Set a reminder for the 9am call." Score on tool selection (calendar vs. web search), action completion.
- **marketing:** "Three comparable cozy-sim launches — their Steam tags, price, and what their first trailer led with." Score on real/current data, source links, no hype.
- **coder:** "Find the bug in this function and propose a fix." Score on diagnosis accuracy, fix correctness.
- **writer:** "Draft a 200-word product description for X in voice Y." Score on voice match, word count compliance, factual accuracy.

Run the suite, log results, repeat weekly. Patterns will emerge — which agents drift, which improve as their skill base grows, where the SOUL.md needs sharpening.

### Layer 3 — the learning loop (the actual point of Hermes)

This is what differentiates Hermes from other agent frameworks. Track over a month:

- **Skill creation.** How many skills did each agent autonomously create? Inspect `~/.hermes/profiles/<name>/skills/` weekly. Are they useful or noise?
- **Skill reuse.** When the same kind of task recurs, does the agent reuse a skill it created earlier, or recreate it from scratch?
- **Memory accumulation.** Check `~/.hermes/profiles/<name>/memories/USER.md` and `MEMORY.md` over time. Is the agent building an accurate model of you and the work, or accumulating noise?
- **Cross-session continuity.** Reference something from a prior conversation without re-explaining it. Does the agent pick it up?
- **Knowledge hygiene.** Sample durable facts and reports in Notion: are they deduplicated, in the correct canonical surface, source-attributed, and linked rather than copied across domains?

The learning loop is the long-tail value. A one-shot benchmark misses the entire point. Plan to evaluate this over weeks, not minutes.

---


## 14. Upgrade and maintenance

Native install = **one `hermes` binary** for all profiles. Upgrade via the same channel you installed from (Homebrew formula or the official installer), then restart the gateways.

**The single-install trade-off — name it:** one binary means **you cannot stage per-agent upgrades.** Upgrading bumps *every* profile at once (the container-per-agent draft could roll one agent and soak it; native can't). So upgrade **deliberately**, on a quiet day, and be ready to roll back the whole install if a release misbehaves.

**Routine upgrade (monthly, or when a release you want ships):**

```bash
# 1. note the current version in case you roll back
hermes --version

# 2. upgrade the binary
brew upgrade hermes-agent          # or rerun the official installer

# 2b. re-add the pip extras — brew upgrade replaces the formula's venv, which
#     drops them. Without python-telegram-bot the bots go silent; without ddgs
#     the built-in web_search has no backend (agents report "no web tools");
#     websockets silences a browser_dialog import warning.
#     Then verify with `hermes doctor`.
$(brew --prefix hermes-agent)/libexec/bin/python -m pip install python-telegram-bot ddgs websockets

# 3. restart every gateway so they pick up the new binary
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway-researcher
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway-general
# ...repeat per loaded profile (or `launchctl list | grep ai.hermes` to enumerate)

# 4. smoke-test: message 2–3 agents, tail their logs
tail -n 50 ~/.hermes/profiles/researcher/logs/gateway.log
```

The state under `~/.hermes/profiles/<profile>/` is untouched — skills, memories, sessions, config all survive; only the binary changes. **Honcho + SearXNG** upgrade separately, via Docker (`docker compose pull && docker compose up -d` — Section 7); keep their image tags pinned, not floating on `latest`.

**Rollback:**

If an upgrade breaks the fleet, reinstall the previous version and restart:

```bash
brew install hermes-agent@<previous-version>   # or reinstall the prior installer build
# then kickstart the profiles as above
```

Because it's one binary, rollback is **all-or-nothing** — there is no per-agent revert. This is the cost of the single-install simplicity; the upside is there is only ever one version to reason about.

**Backup strategy:**

See Section 9.7 for the full memory backup plan. In summary: `~/.hermes/profiles/*/memories/`, `~/.hermes/profiles/*/sessions/`, and `~/.hermes/profiles/*/skills/` go into whatever you already back up (Time Machine, Restic, rsync) — Time Machine on the Mini already covers `~/.hermes/` if enabled. The Honcho Postgres needs a weekly `pg_dump`. Notion is external SaaS state: local Hermes/Honcho backups do not back up its page/database contents, and local OAuth files are credentials rather than data backups. Verify at least one restore/export path for every state plane before relying on it.

**Log management:**

Per-profile logs live at `~/.hermes/logs/gateways/<name>/current` (Hermes rotates these — 10 archives × 1 MB). launchd stdout/stderr at `~/.hermes/logs/launchd/` grow unbounded; add a monthly rotation if disk becomes an issue.

---


## 14.5 Fleet health alerting — a dumb watchdog (no agent needed)

There is **no dedicated ops agent for host monitoring** — that is deliberately not an LLM's job. Config/fleet administration belongs to `general`/Derya, with an explicit show-then-confirm behavioral rule. The health path remains a **dumb watchdog**: a launchd job checks all nine gateway labels and the four Honcho containers every five minutes, tracks PID changes, and sends only when state changes.

The canonical script is [`scripts/watchdog.sh`](../scripts/watchdog.sh), deployed at `~/.hermes/scripts/watchdog.sh`. It never reads `.env` files or calls the raw Telegram Bot API. Delivery goes through `~/.hermes/scripts/hermes-send-keychain.sh general --to telegram`, which sanitizes its environment before Keychain lookup, obtains the profile-scoped 1Password service-account token, resolves only `TELEGRAM_*` references through the Quicksilver SDK bootstrap, removes the bootstrap token, and execs managed Hermes `send`. The maintenance boundary accepts only the configured Telegram home target and piped stdin or literal text; arbitrary chat IDs, local files, and media attachments are rejected.

A changed PID means launchd restarted the gateway — exactly the event `KeepAlive` would otherwise hide. Repeated identical state is suppressed; the first healthy baseline is silent. Gateway and Honcho-container failures are alert-only: launchd/OrbStack own recovery, while the watchdog makes failure visible.

Schedule it at `~/Library/LaunchAgents/ai.hermes.watchdog.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>            <string>ai.hermes.watchdog</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/YOU/.hermes/scripts/watchdog.sh</string>
    </array>
    <key>StartInterval</key>    <integer>300</integer>
</dict>
</plist>
```

**Test it once** (the runbook has this as a gate): `launchctl bootout` the researcher gateway, wait ≤5 min for the Telegram alert, `bootstrap` it back.

**Known limit:** a same-machine watchdog can't report the machine dying (power, kernel panic, no network). If you add a dead-man service, treat its ping UUID/URL as a credential: store it in 1Password and resolve it at runtime rather than embedding it in the script, plist or documentation. Optional; the Telegram path already covers the common failures.

**Current scope:** all nine gateways and the four Honcho containers. SearXNG remains outside this watchdog and is checked through its own service health path.

---

## 14.6 Startup "online" notification — per-bot greeting at boot

The watchdog (14.5) is *anomaly* alerting — it speaks up on down/crash-loop. It does **not** announce a healthy startup. And Hermes' own restart message only reaches *recently-active* chats (it calls `notify_active_sessions` on SIGTERM; an idle bot has `active_at_start=0`, so e.g. Sarp gets nothing when you restart it cold). To get a deterministic "I'm up" line in **every** bot's own chat when the fleet starts, add a one-shot launchd job — same dumb-pipe pattern as the watchdog, no agent involved.

`scripts/notify-online.sh` (in this repo — deploy to `~/.hermes/scripts/`, same as `wire-tinyfish.sh`) checks all launchd labels and asks the Quicksilver-aware `hermes-send-keychain.sh general --to telegram` wrapper to deliver one fleet-level status message. The helper does not read or fan out Telegram tokens; it resolves only the general profile's `TELEGRAM_*` references. It waits briefly for gateways before checking them.

```bash
notify-online.sh           # all nine (what the launchd job runs)
notify-online.sh producer  # just Sarp — after a single-gateway kickstart
```

Fire it at boot/login with `~/Library/LaunchAgents/ai.hermes.fleet-online.plist` — one-shot (`RunAtLoad: true`, `KeepAlive: false`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
    <key>Label</key>            <string>ai.hermes.fleet-online</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/YOU/.hermes/scripts/notify-online.sh</string>
        <string>all</string>
    </array>
    <key>RunAtLoad</key>        <true/>
    <key>KeepAlive</key>        <false/>
    <key>StandardOutPath</key>  <string>/Users/YOU/.hermes/scripts/fleet-online.log</string>
    <key>StandardErrorPath</key><string>/Users/YOU/.hermes/scripts/fleet-online.log</string>
</dict>
</plist>
```

Load with `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.fleet-online.plist`; that also runs it once, which is the live test (you should see nine pings, one per bot chat).

**Scope / limit:** `RunAtLoad` covers machine boot and login. A manual `launchctl kickstart -k` of a *single* gateway does **not** re-trigger this job (separate launchd label) — run `notify-online.sh <slug>` by hand for that one, or re-kick the whole job with `launchctl kickstart gui/$(id -u)/ai.hermes.fleet-online`. Per-restart auto-fire would mean wrapping each Hermes-generated `gateway-<slug>.plist`, which an upgrade clobbers — not worth the fragility for a greeting.

---


## 15. Open questions to revisit in week 2

These are decisions worth deferring until you have real usage data:

- **Does `coder` need its own machine after all?** It runs native on the Mini for the Metal GPU. If Godot builds start starving the always-on agents (no per-profile RAM cap exists natively — Section 1.1), the clean escalation is to bring the shelved MacBook Pro back online as a dedicated `coder` host — which also restores `coder`'s credential isolation from the fleet ([Section 13](09-security.md)). Revisit only if you feel the resource or blast-radius pressure.
- **Do you ever need a real ops agent?** Probably not — the watchdog (§14.5) owns the critical alert path deterministically. Only build one if you start wanting natural-language host *diagnosis* ("why is X slow / what's eating RAM") often enough that manual checks get tedious. (`general`/Derya now has an admin shell for *config* tuning — but host *diagnosis* is still a manual ask or a future agent.) If you do build one, it's another shell agent — fence it like `coder` (§13) and never give it the Docker socket.
- **Local inference?** Currently everything goes to remote API providers. With nine agents the bill adds up — at some point a local 7B model for the cheap tasks (assistant title generation, Honcho deriver) makes sense. Revisit once you have a month of usage data.
- **`producer` scoring cadence.** Sarp is live but its weekly backlog-scoring cron (Section 16) isn't wired yet — add it once the scout's digests actually accumulate candidates worth scoring on a schedule.

---
