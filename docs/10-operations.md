# Operations — Eval, Maintenance, Open Questions

[← All docs](../README.md)

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
- **Local inference?** Currently everything goes to remote API providers. With seven agents the bill adds up — at some point a local 7B model for the cheap tasks (ops, concierge title generation) makes sense. Revisit once you have a month of usage data.
- **Sixth agent's role?** Earmarked for `producer`, the game-development scoring agent (Section 16) — but **deferred**. Phase A is research-only (a single cron on the `research` agent). Build `producer` into the slot only when the opportunity backlog outpaces hand-curation. Until then the slot stays open.

---
