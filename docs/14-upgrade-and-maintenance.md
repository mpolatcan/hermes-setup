# Upgrade & Maintenance Runbook

[← All docs](../README.md)

Live procedures for keeping the fleet healthy across hermes-agent upgrades, plus the 2026-07-05 maintenance findings that produced them.

---

## 20. The upgrade checklist

`brew upgrade hermes-agent` alone **breaks the whole fleet**. Two hardcoded-path traps:

1. **Gateway plists pin the versioned Cellar path** (`/opt/homebrew/Cellar/hermes-agent/<ver>/libexec/bin/python`). The old dir is deleted on upgrade; every gateway dies on next restart with nothing in the error log.
2. **macOS Full Disk Access is granted per versioned Python.app** (`/opt/homebrew/Cellar/python@3.14/<ver>/…/Python.app`). Upgrades bump python@3.14 as a dependency → the grant silently goes stale → TCC permission prompts return.

Full procedure, in order, from a **full login shell** (the plist PATH is snapshotted from the installing shell — a minimal shell bakes a PATH that can't see `~/.local/bin` / `/opt/homebrew/bin`, and the agents lose `claude`, `codex`, `ntn`):

```bash
brew upgrade hermes-agent

# 1. regenerate + reload all gateway plists
for p in assistant coder finance general health marketing producer researcher writer; do
  hermes --profile $p gateway install --force
  launchctl bootout gui/501/ai.hermes.gateway-$p; sleep 2
  launchctl bootstrap gui/501 ~/Library/LaunchAgents/ai.hermes.gateway-$p.plist
done
# bootout needs a few seconds to settle; "Bootstrap failed: 5" → sleep 4 and retry

# 2. migrate config schema on ALL profiles (doctor --fix only does the active one)
for p in assistant coder finance general health marketing producer researcher writer; do
  hermes -p $p config migrate
done

# 3. rebuild the dashboard frontend to match the backend
cd ~/hermes-dashboard-src && git fetch --tags && git checkout v<new-version>
cd web && npm ci && npm run build          # v0.18+: `git sparse-checkout add apps` once (@hermes/shared)
launchctl kickstart -k gui/501/ai.hermes.dashboard

# 4. restart fleet so migrated configs load
for p in …; do launchctl kickstart -k gui/501/ai.hermes.gateway-$p; done

# 5. FDA re-grant if python@3.14 bumped (check: ls /opt/homebrew/Cellar/python@3.14/)
#    System Settings → Privacy & Security → Full Disk Access → drag the NEW Python.app, drop the old entry
#    then restart the fleet again (grants apply to new processes only)

# 6. verify
hermes gateway list                        # 9 ✓
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:9119/   # 200
hermes -p coder -z "which claude codex ntn"                     # all three resolve
```

Done this way for 0.16.0 → 0.18.0 (2026.7.1) on 2026-07-05.

## 21. Backup system — current state (2026-07-05)

One deterministic script owns Honcho dumps: `~/honcho-stack/backup-honcho.sh` (mirrored at `~/.hermes/scripts/backup-honcho.sh`, copy in `scripts/` here).

- Runs twice daily: launchd `ai.hermes.backup-honcho` at 03:00 + general-profile cron job `129cac421e8c` at 04:00 (**no-agent script mode** — the script is the job).
- Hardened after the June incident: container-running check → dump to temp file → verify ≥10 KB → publish; rotation keeps 14 (≈7 days at 2/day); failures log to `~/backups/honcho-backup.log` **and** alert via `hermes send`.
- **Why hardened:** OrbStack was down June 17 → July 4 and nobody noticed. The old script piped a failing `pg_dump` into `gzip`, producing 18 consecutive 20-byte "backups"; one more successful rotation would have deleted the last good dumps. Separately, an **LLM-agent** version of this cron fabricated a success report ("145 MB, 13 s, MD5 …") for a file that never existed, and its ambiguous cleanup prompt ("Son 7 günlük yedekleri temizle") deleted fresh dumps. **Rule: an agent never owns backup or rotation logic — scripts do the work, agents at most narrate.**
- Weekly state backup (`ai.hermes.backup-state`, Sat 04:00): profile configs + memories + logs → `~/.hermes/state-backups/hermes-state-*.tar.gz`, plus root-level `hermes-root-*.tar.gz` (honcho.json, kanban.db, active_profile, root config.yaml, SOUL.md, scripts/). 28-day retention. Deliberately excludes `state.db` (GB-scale session history, not config).
- Weekly skills backup (curator cron, Sat 03:00): registry JSON export **plus** `shared-skills-<date>.tar.gz` of `~/.hermes/shared-skills/canonical` — the JSON export alone covers only hub-installed skills, which is none of ours; the tar covers the 16 real local skills. Keeps 4 of each. The cron's script path is the **profile** scripts dir: `~/.hermes/profiles/general/scripts/curator-snapshot.sh`.

## 22. Watchdog v2

`~/.hermes/scripts/watchdog.sh` (5-min launchd interval) now also checks the four Honcho containers (`server-api-1`, `server-database-1`, `server-redis-1`, `server-deriver-1`) via `docker inspect` — alert-only; containers are `unless-stopped`, so recovery is automatic once OrbStack is back, but a down OrbStack needs a human.

Two bugs fixed 2026-07-05:
- `grep -o '-*[0-9]*$'` — a dash-leading pattern is parsed as a grep **option**, so LastExitStatus parsing had been broken since the script was written; crash detection (nonzero-exit alerts) never actually worked. Fixed with `grep -o --`.
- macOS has no `timeout(1)` — a `timeout 10 docker inspect …` guard silently failed and produced one false "all containers down" Telegram alert before removal.

## 23. Honcho base URL — localhost, never a LAN IP

The Honcho API binds `127.0.0.1:8000`. Root `~/.hermes/honcho.json` once pointed at an OrbStack subnet IP (`192.168.107.5`) — those rotate (`.4` → `.5` → dead), and a LAN-IP target also trips macOS **Local Network** TCC prompts against Python. All profile-level honcho.json files and the root file now use `http://localhost:8000`. If memory misbehaves: `docker ps` first, then confirm no config regressed to a subnet IP.

## 24. Session-store hygiene

Cron-agent runs create sessions like any other conversation. Eight hourly "downtime-recovery" jobs had quietly grown `profiles/general/state.db` to **3.1 GB** (4726 of 4778 sessions were cron transcripts; the FTS trigram index alone was 1.2 GB). Cleanup that reclaimed ~2.3 GB across the fleet:

```bash
hermes -p <profile> sessions prune --source cron --older-than 7 --yes
hermes -p <profile> sessions optimize     # FTS merge + VACUUM
```

Worth running monthly, or whenever `hermes sessions stats` shows cron sessions dominating.

## 25. Fleet config versioning — git at ~/.hermes (2026-07-05)

The hand-tuned config surface is a git repo inside `~/.hermes` (whitelist `.gitignore`: 9× config.yaml/SOUL.md/honcho.json/cron-jobs.json, 16 canonical skills, ops scripts, root config — never `.env`/`auth.json`/state.db/sessions/memories). Meaningful edits get manual commits; a daily 02:30 launchd job (`ai.hermes.config-snapshot`, plain script — never an agent) auto-commits drift from agent edits and hermes migrations, and **refuses to commit if secret-shaped content appears in tracked files** (Telegram alert instead).

Rollback of a persona/skill/cron change:
```bash
cd ~/.hermes
git log --oneline -- profiles/coder/SOUL.md   # what changed when
git checkout <sha> -- profiles/coder/SOUL.md  # targeted restore
launchctl kickstart -k gui/501/ai.hermes.gateway-coder
```
Sync direction is always **live → this docs repo** (scripts copied at doc-update time); the ~/.hermes repo is the machine-state journal, this repo the curated record.

## 26. Skill consolidations — cron refs are the blast radius

June 2026 skill-curation merges renamed skills without updating cron job `skills:` lists, so jobs ran for weeks with `skill not found` warnings while agents improvised the procedure (same failure family as the fabricating backup agent). The absorption map recovered from the successors' own SKILL.md notes:

| dead name | absorbed into |
|---|---|
| `cron-message-format` | `agent-message-formats` |
| `memory-eviction` | `hermes-memory-hygiene` |
| `knowledge-maintenance` | `notion-knowledge-ops` |
| `honcho-to-notion` | `notion-knowledge-ops` |

All 23 affected job definitions repointed 2026-07-05. Also found and fixed a **double eviction**: general's jobs.json held central `mem-eviction-<profile>` copies for all 9 profiles while each profile also had a local twin — both ran nightly, 30–45 min apart (central copies now paused, locals canonical). After any future skill merge: sweep every profile's `cron/jobs.json` for the old name AND check profile-local `skills/` dirs before declaring a reference dead — health's medication skills live in `profiles/health/skills/`, not canonical.
