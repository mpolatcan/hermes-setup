# Upgrade & Maintenance Runbook

[← All docs](../README.md)

Live procedures for keeping the fleet healthy across hermes-agent upgrades, plus the 2026-07-05 maintenance findings that produced them.

---

## 20. The upgrade checklist

The production fleet does not execute Homebrew Hermes. Gateways, operational scripts, the shell CLI shim, and the dashboard use the stable managed runtime:

```text
/Users/mutlupolatcan/.hermes/runtime/hermes-agent
└── venv/bin/hermes  # Python 3.13; stable launchd target
```

Do not upgrade the live directory in place and do not make Python `3.14` the production runtime. Stage the official candidate side by side, preserve the stable path for rollback, and keep the candidate and Honcho adapter in the same supported Python `major.minor` family (`3.13`). The adapter compatibility gate is documented in [`integrations/honcho-codex-adapter/docs/upgrade-lifecycle.md`](../integrations/honcho-codex-adapter/docs/upgrade-lifecycle.md).

Full procedure, in order:

```bash
# 1. Stage the official tagged candidate under a versioned sibling path.
#    Use Python 3.13 and run package/import/pip-check plus adapter compatibility gates.
CANDIDATE=/Users/mutlupolatcan/.hermes/runtime/hermes-agent-candidate-v<release>
test -x "$CANDIDATE/venv/bin/hermes"
"$CANDIDATE/venv/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 13)'

# 2. Run a non-production gateway canary and the adapter compatibility suite.
#    Do not modify the stable runtime or production launchd jobs yet.

# 3. Build and start the candidate dashboard on 9120 in terminal A.
env -u HERMES_WEB_DIST \
  "$CANDIDATE/venv/bin/hermes" -p general dashboard \
  --no-open --host 127.0.0.1 --port 9120

# In terminal B, smoke-test it, then stop the terminal-A canary.
curl -fsS http://127.0.0.1:9120/ >/dev/null

# 4. After explicit approval, back up the stable runtime and promote the
#    verified candidate so the stable path remains the launchd contract.

# 5. Migrate all nine configs, then restart the eight auxiliary gateways in
#    sequence. Restart general separately only after explicit approval.

# 6. Point HERMES_WEB_DIST at the promoted runtime, lint the dashboard plist,
#    then reload only the dashboard job. launchd needs a drain interval.
plutil -lint /Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.dashboard.plist
launchctl bootout gui/501/ai.hermes.dashboard
sleep 3
launchctl bootstrap gui/501 /Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.dashboard.plist

# 7. Verify disk package, live processes, ports, HTTP, and the global CLI as
#    separate surfaces. All nine gateway commands must resolve below the
#    stable managed runtime and the dashboard must return HTTP 200.
/Users/mutlupolatcan/.hermes/runtime/hermes-agent/venv/bin/hermes --version
command -v hermes && hermes --version
curl -fsS http://127.0.0.1:9119/ >/dev/null
```

The managed-runtime pattern was exercised for the `0.19.0` / `v2026.7.20` Quicksilver rollout on 2026-07-23–24. Homebrew `0.18.2` remains installed only as a rollback surface; no production gateway or dashboard executes it.

## 21. Backup system — current state (2026-07-05)

One deterministic script owns Honcho dumps: `~/honcho-stack/backup-honcho.sh` (mirrored at `~/.hermes/scripts/backup-honcho.sh`, copy in `scripts/` here).

- Runs twice daily: launchd `ai.hermes.backup-honcho` at 03:00 + general-profile cron job `129cac421e8c` at 04:00 (**no-agent script mode** — the script is the job).
- Hardened after the June incident: container-running check → dump to temp file → verify ≥10 KB → publish; rotation keeps 14 (≈7 days at 2/day); failures log to `~/backups/honcho-backup.log` **and** alert via `hermes send`.
- **Why hardened:** OrbStack was down June 17 → July 4 and nobody noticed. The old script piped a failing `pg_dump` into `gzip`, producing 18 consecutive 20-byte "backups"; one more successful rotation would have deleted the last good dumps. Separately, an **LLM-agent** version of this cron fabricated a success report ("145 MB, 13 s, MD5 …") for a file that never existed, and its ambiguous cleanup prompt ("delete the last seven days of backups") deleted fresh dumps. **Rule: an agent never owns backup or rotation logic — scripts do the work, agents at most narrate.**
- Weekly state backup (`ai.hermes.backup-state`, Sat 04:00): profile configs + memories + logs → `~/.hermes/state-backups/hermes-state-*.tar.gz`, plus root-level `hermes-root-*.tar.gz` (honcho.json, kanban.db, active_profile, root config.yaml, SOUL.md, scripts/). 28-day retention. Deliberately excludes `state.db` (GB-scale session history, not config).
- Weekly skills backup (curator cron, Sat 03:00): registry JSON export **plus** `shared-skills-<date>.tar.gz` of `~/.hermes/shared-skills/canonical` — the JSON export alone covers only hub-installed skills, which is none of ours; the tar covers the 16 real local skills. Keeps 4 of each. The cron's script path is the **profile** scripts dir: `~/.hermes/profiles/general/scripts/curator-snapshot.sh`.
- **Notion boundary:** these local archives do not back up Notion page/database contents. Notion is the external durable knowledge/reporting plane; its OAuth directory is sensitive access state, not a content backup. Workspace retention/export is a separate control ([docs/16](16-notion-knowledge-and-reporting.md)).

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

## 27. Runtime topology remediation and gated retirement (2026-07-25)

The production installation remains intentionally split by responsibility, not by competing agent cores:

- `~/.hermes/runtime/hermes-agent` is the only production Hermes code and Python environment.
- `~/.hermes/profiles/<name>` owns profile state and configuration.
- `~/Library/LaunchAgents` owns macOS service definitions.
- `~/.hermes/scripts` owns machine-level deterministic cron scripts.
- This repository is the canonical, reviewable source for deployment artifacts and local Hermes patches.

The July 25 audit removed the obsolete `ai.hermes.linear-bridge` LaunchAgent and its unused runtime. The native Linear plugin remains on `127.0.0.1:8787` and continues to own the existing credential, OAuth, and SQLite state files. Unsigned local Desktop `dist/` and `release/` artifacts were deleted; no Hermes Desktop app is installed in `/Applications`.

The dashboard CLI status detector did not recognize the machine dashboard command when `-p default` preceded `dashboard`. The verified local production commit is preserved as an applyable mail patch:

```text
patches/hermes-agent/0001-fix-cli-detect-profile-scoped-dashboard-processes.patch
```

Apply it only to a compatible Hermes Agent checkout and run the focused stale-dashboard, lifecycle, and Windows subprocess tests before promotion. The patch structurally validates supported launchers instead of substring-matching arbitrary process text; this is security-sensitive because the same detector is used by the update kill path.

Quicksilver retirement is deliberately gated:

1. `integrations/honcho-codex-adapter/scripts/quicksilver_soak_cleanup.py` is the byte-for-byte source copy of the deployed canonical soak cleanup. It removes the recovery checkout and managed candidate only after the scheduler reaches 8 completed runs and the persisted soak history contains seven successful calendar days with no failures.
2. The remaining Python canary, gateway/adapter candidates, Homebrew formula, and Homebrew-backup environments are **not** deleted by unattended automation. After the canonical completion marker exists, re-run live process, `lsof`, LaunchAgent, cron, config, global-CLI, and path-safety checks; then show the exact `brew uninstall` and removal commands for explicit approval. Do not infer approval from the earlier soak schedule.

The canonical cleanup remains general-profile no-agent job `c083e57807d7` at 04:40. A proposed destructive post-soak job was rejected during independent review and removed before its first run; no post-cleanup script remains deployed.

The npm audit remains advisory-only for the unresolved build surfaces. The installed TUI graph already resolves direct `eslint-plugin-react` to `7.37.5`; npm's remaining automatic remediation requires incompatible actions (React Router/`electron-builder` downgrades or ESLint major changes). Do not use `npm audit fix --force`. Re-evaluate when upstream publishes compatible patched releases, then require web/TUI/Desktop typecheck, test, and build gates before committing lockfile changes.
