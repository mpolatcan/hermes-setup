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
- Gateway reconnection working: kill Wi-Fi briefly, confirm gateways reconnect

### Layer 2 — per-agent quality

Build a 15–30 task personal eval suite. For each agent, define 3–5 representative tasks with known-good outcomes. Examples:

- **research:** "Find three recent peer-reviewed papers on X, summarize each in 2 sentences, link each." Score on source quality, summary accuracy, citation correctness.
- **assistant:** "What's on my calendar tomorrow morning? Set a reminder for the 9am call." Score on tool selection (calendar vs. web search), action completion.
- **ops:** "Check disk usage on this host. Alert me if any volume is over 80%." Score on correct command, correct interpretation.
- **coder:** "Find the bug in this function and propose a fix." Score on diagnosis accuracy, fix correctness.
- **writer:** "Draft a 200-word product description for X in voice Y." Score on voice match, word count compliance, factual accuracy.

Run the suite, log results, repeat weekly. Patterns will emerge — which agents drift, which improve as their skill base grows, where the SOUL.md needs sharpening.

### Layer 3 — the learning loop (the actual point of Hermes)

This is what differentiates Hermes from other agent frameworks. Track over a month:

- **Skill creation.** How many skills did each agent autonomously create? Inspect `~/.hermes/profiles/<name>/skills/` weekly. Are they useful or noise?
- **Skill reuse.** When the same kind of task recurs, does the agent reuse a skill it created earlier, or recreate it from scratch?
- **Memory accumulation.** Check `~/.hermes/profiles/<name>/memories/USER.md` and `MEMORY.md` over time. Is the agent building an accurate model of you and the work, or accumulating noise?
- **Cross-session continuity.** Reference something from a prior conversation without re-explaining it. Does the agent pick it up?

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

# 3. restart every gateway so they pick up the new binary
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway-research
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway-general
# ...repeat per loaded profile (or `launchctl list | grep ai.hermes` to enumerate)

# 4. smoke-test: message 2–3 agents, tail their logs
tail -n 50 ~/.hermes/profiles/research/logs/gateway.log
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

See Section 9.7 for the full memory backup plan. In summary: `~/.hermes/profiles/*/memories/`, `~/.hermes/profiles/*/sessions/`, and `~/.hermes/profiles/*/skills/` go into whatever you already back up (Time Machine, Restic, rsync) — Time Machine on the Mini already covers `~/.hermes/` if enabled. The Honcho Postgres needs a weekly `pg_dump`. Verify at least one restore works before relying on it.

**Log management:**

Per-profile logs live at `~/.hermes/logs/gateways/<name>/current` (Hermes rotates these — 10 archives × 1 MB). launchd stdout/stderr at `~/.hermes/logs/launchd/` grow unbounded; add a monthly rotation if disk becomes an issue.

---


## 14.5 Phase-A health alerting — a watchdog before Nilay exists

`ops` (Nilay) is deferred to Phase B, so in Phase A **nothing tells you a gateway is down**. `KeepAlive` restarts a crashed process but *masks crash-loops*, and the Layer-1 checks (Section 12) are weekly-manual — up to a week blind. Close the gap with a dumb watchdog that bypasses the agents entirely: a launchd job that checks the fleet every 15 minutes and, on failure, messages you through the **raw Telegram Bot API**. It must not depend on any agent — it has to work precisely when they don't.

`~/.hermes/scripts/watchdog.sh`:

```bash
#!/bin/bash
# watchdog.sh — gateway health → Telegram, bypassing the agents.
# Reuses the general bot's token; talks straight to api.telegram.org.
set -u
EXPECTED=(research)                      # extend as profiles go live
ENV="$HOME/.hermes/profiles/general/.env"
TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV" | cut -d= -f2)
CHAT=$(grep '^TELEGRAM_ALLOWED_USERS=' "$ENV" | cut -d= -f2 | cut -d, -f1)
STATE=/tmp/hermes-watchdog; mkdir -p "$STATE"

alerts=()
for p in "${EXPECTED[@]}"; do
  pid=$(launchctl list | awk -v l="ai.hermes.gateway-$p" '$3==l {print $1}')
  if [ -z "$pid" ] || [ "$pid" = "-" ]; then
    alerts+=("$p: DOWN")
  elif [ -f "$STATE/$p" ] && [ "$pid" != "$(cat "$STATE/$p")" ]; then
    alerts+=("$p: restarted since last check (crash-loop?)")
  fi
  echo "${pid:-none}" > "$STATE/$p"
done

if [ ${#alerts[@]} -gt 0 ]; then
  curl -fsS "https://api.telegram.org/bot$TOKEN/sendMessage" \
    --data-urlencode chat_id="$CHAT" \
    --data-urlencode text="⚠️ hermes watchdog: ${alerts[*]}" >/dev/null
fi
```

Two checks per profile: the launchd job has a live PID, and the PID hasn't changed since the last run — a changed PID means launchd restarted the gateway, which is exactly the event `KeepAlive` would otherwise hide. While something is down it re-alerts every 15 minutes; that nagging is a feature, not a bug. (The `sendMessage` works because you've already messaged the bot — Telegram bots can't initiate chats otherwise.)

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
    <key>StartInterval</key>    <integer>900</integer>
</dict>
</plist>
```

**Test it once** (the runbook has this as a gate): `launchctl bootout` the research gateway, wait ≤15 min for the Telegram alert, `bootstrap` it back.

**Known limit:** a same-machine watchdog can't report the machine dying (power, kernel panic, no network). If you want that too, add one line at the end of a *successful* run — `curl -fsS https://hc-ping.com/<uuid>` against a free dead-man service (e.g. healthchecks.io), which emails you when the pings stop. Optional; the Telegram path already covers the common failures (gateway crash-loop, broken config after an upgrade).

**Phase B:** extend `EXPECTED`, and add a `docker compose ps` check so Honcho + SearXNG count as fleet members.

---


## 15. Open questions to revisit in week 2

These are decisions worth deferring until you have real usage data:

- **Does `coder` need its own machine after all?** It runs native on the Mini for the Metal GPU. If Godot builds start starving the always-on agents (no per-profile RAM cap exists natively — Section 1.1), the clean escalation is to bring the shelved MacBook Pro back online as a dedicated `coder` host — which also restores `coder`'s credential isolation from the fleet ([Section 13](09-security.md)). Revisit only if you feel the resource or blast-radius pressure.
- **Should `ops` get Honcho after all?** Section 9 keeps it off because determinism is the goal. After a month, if `ops` feels too generic or repeats explanations you've given before, flip it on with `aiPeer: "ops"`.
- **Local inference?** Currently everything goes to remote API providers. With seven agents the bill adds up — at some point a local 7B model for the cheap tasks (ops, assistant title generation) makes sense. Revisit once you have a month of usage data.
- **When to build `producer`?** Sarp, the game-development scoring agent (Section 16), is spec'd but **deferred**. Phase A is research-only (a single cron on the `research` agent). Stand up the `producer` profile only when the opportunity backlog outpaces hand-curation. Until then it stays unbuilt.

---
