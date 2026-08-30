# Upgrade & Maintenance Runbook

[← All docs](../README.md)

Live procedures for keeping the fleet healthy across hermes-agent upgrades, plus the 2026-07-05 maintenance findings that produced them.

---

## 20. The upgrade checklist

The production fleet does not execute Homebrew Hermes. Gateways and the shell CLI resolve an updater-managed immutable release; discover the active release from the live process or `hermes --version`, never from a hard-coded hash:

```text
/Users/mutlupolatcan/.local/bin/hermes
└── ~/.hermes/runtime/releases/hermes-agent-<managed-hash>/venv/bin/hermes
```

Do not upgrade the live directory in place and do not make Python `3.14` the production runtime. Stage the official candidate side by side, preserve the stable path for rollback, and keep the candidate and Honcho adapter in the same supported Python `major.minor` family (`3.13`). The adapter compatibility gate is documented in [`components/memory/honcho-codex-bridge/docs/upgrade-lifecycle.md`](../components/memory/honcho-codex-bridge/docs/upgrade-lifecycle.md).

Full procedure, in order:

```bash
set -euo pipefail

# 1. Fetch official origin/main, record its exact commit, and stage that exact
#    commit under a versioned sibling path. Use Python 3.13 and run
#    package/import/pip-check plus adapter compatibility gates.
git -C /absolute/path/to/official-hermes-checkout fetch origin
TARGET_SHA=$(git -C /absolute/path/to/official-hermes-checkout rev-parse origin/main)
CANDIDATE=/Users/mutlupolatcan/.hermes/runtime/hermes-agent-candidate-v<release>
test -x "$CANDIDATE/venv/bin/hermes"
"$CANDIDATE/venv/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 13)'
test "$(git -C "$CANDIDATE" rev-parse HEAD)" = "$TARGET_SHA"

# 2. Run a non-production gateway canary and the adapter compatibility suite.
#    Do not modify the stable runtime or production launchd jobs yet.

# 3. Build and start the candidate dashboard on 9120 in terminal A.
env -u HERMES_WEB_DIST \
  "$CANDIDATE/venv/bin/hermes" -p general dashboard \
  --no-open --host 127.0.0.1 --port 9120

# In terminal B, smoke-test it, then stop the terminal-A canary.
curl -fsS http://127.0.0.1:9120/ >/dev/null

# 4. Take an independent native quick backup and require its manifest. Then
#    fetch again and require origin/main to be the same tested commit. If it
#    moved, stop and restage/retest. After explicit approval, use the official
#    updater with its additional backup. Do not carry local core commits.
BACKUP="/Users/mutlupolatcan/.hermes/backups/pre-update-${TARGET_SHA}.zip"
test ! -e "$BACKUP"
hermes backup --quick --output "$BACKUP"
test -s "$BACKUP"
unzip -t "$BACKUP" >/dev/null
git -C /absolute/path/to/official-hermes-checkout fetch origin
test "$(git -C /absolute/path/to/official-hermes-checkout rev-parse origin/main)" = "$TARGET_SHA"
hermes update --yes --backup

# The updater performs its own fetch and cannot pin TARGET_SHA. Before any
# gateway restart, fail closed unless the promoted checkout is exactly the
# tested commit. On mismatch, serve nothing new: restore the previous managed
# release/backup, then stage and test the new upstream target.
PROMOTED_SHA=$(git -C /absolute/path/to/promoted-hermes-agent rev-parse HEAD)
if [[ "$PROMOTED_SHA" != "$TARGET_SHA" ]]; then
  echo "Promoted SHA differs from tested candidate; do not restart any gateway." >&2
  echo "Restore the previous managed release/backup, then restage the new target." >&2
  exit 1
fi

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
~/.local/bin/hermes --version
command -v hermes && hermes --version
curl -fsS http://127.0.0.1:9119/ >/dev/null
```

The current accepted core is official Hermes Agent `v0.20.3` (`2026.8.16.2`) on Python `3.13.15`. Its active managed release matches `NousResearch/hermes-agent` `origin/main` with zero local commits and zero behavioral source diff. Local capabilities belong in profile plugins/config, not updater-managed release directories.

### Preserve the 07:00 Telegram session boundary

Hermes changed `SessionResetPolicy.mode` from `both` to `none` in July 2026 so conversations persist by default. An omitted reset policy therefore no longer preserves this fleet's historical daily rollover. Keep the intent explicit in every profile config:

```yaml
session_reset:
  mode: daily
  at_hour: 7
  notify: true
```

`at_hour` uses the host's local time. After each Hermes upgrade, resolve the effective `GatewayConfig.default_reset_policy` for all nine profiles and verify that the next Telegram message after 07:00 creates a new session with the previous active session ended as `session_reset`. Retention pruning is separate: it removes old ended sessions and must not be treated as active-session rotation.

## 21. Backup system — current state (2026-07-26)

Two deterministic, non-agent layers own backup work:

- **Hermes profile state:** `~/.hermes/scripts/profile-backup-quick.sh`, scheduled by `ai.hermes.backup-state` every day at 01:00. It runs native `hermes backup --quick` for all nine profile homes, requires a fresh `manifest.json`, rejects failed/oversized database entries, verifies every copied SQLite database, and only then keeps the newest seven snapshots per profile.
- **Honcho PostgreSQL:** `~/.hermes/services/honcho-stack/backup-honcho.sh`, scheduled by `ai.hermes.backup-honcho` at 03:00 and 15:00. It checks the database container, writes a partial `pg_dump`, validates size and `gzip -t`, atomically publishes the dump, writes SHA-256 evidence, and applies retention only after success.
- **Watchdog contract:** `watchdog.sh` checks Honcho dump freshness (<16h) and all nine Hermes profile snapshots (<26h, manifest present). It no longer requires a global full ZIP.
- **Retired:** weekly live tar files under `~/.hermes/state-backups`, the root-wide `hermes backup -o` wrapper, `ai.hermes.backup-state-full`, and plaintext partial full archives. The root-wide archive treated reproducible `runtime/`, `source/`, `staging/`, `archive/` and service trees as state and duplicated credential files.
- **Restore evidence:** each scheduled Hermes run verifies snapshots before retention; acceptance includes an isolated copied `state.db` with `PRAGMA integrity_check=ok`. Honcho has its own throwaway-container restore drill.
- **Remaining DR gap:** local snapshots protect rollback, not disk loss. Any future off-host layer must be encrypted, exclude reproducible code/runtime trees, and pass restore acceptance before it can own retention.
- **Notion boundary:** local archives do not back up Notion page/database contents. Notion workspace retention/export remains a separate control ([docs/16](16-notion-knowledge-and-reporting.md)).

## 22. Watchdog v2

`~/.hermes/scripts/watchdog.sh` (5-min launchd interval) now also checks the four Honcho containers (`server-api-1`, `server-database-1`, `server-redis-1`, `server-deriver-1`) via `docker inspect` — alert-only; containers are `unless-stopped`, so recovery is automatic once OrbStack is back, but a down OrbStack needs a human.

Two bugs fixed 2026-07-05:
- `grep -o '-*[0-9]*$'` — a dash-leading pattern is parsed as a grep **option**, so LastExitStatus parsing had been broken since the script was written; crash detection (nonzero-exit alerts) never actually worked. Fixed with `grep -o --`.
- macOS has no `timeout(1)` — a `timeout 10 docker inspect …` guard silently failed and produced one false "all containers down" Telegram alert before removal.

## 23. Honcho base URL — authenticated loopback alias, never a LAN IP

The Honcho API binds `127.0.0.1:8000`, but authenticated Hermes clients use `http://honcho.localhost:8000`. That hostname must resolve exclusively to `127.0.0.1` and/or `::1`. Literal `localhost`/`127.0.0.1` currently selects Hermes' unauthenticated SDK placeholder unless an API key is written explicitly to `honcho.json`; the loopback alias preserves the process-scoped `HONCHO_API_KEY` without persisting a JWT or disabling server auth. All nine profile-local files and the root fallback use the alias at mode `0600`. Acceptance requires container health, vault-root/server-secret hash parity, a fresh scoped-JWT workspace read, a real Hermes peer/card or search read, and zero post-restart `Invalid JWT` errors. `/health` alone is insufficient.

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

## 27. Runtime topology and extension ownership

Production is intentionally split by responsibility:

- `~/.hermes/runtime/releases/hermes-agent-<hash>` is updater-owned immutable Hermes core.
- `~/.local/bin/hermes` selects the active managed release.
- `~/.hermes/profiles/<name>` owns profile state, configuration and installed plugins.
- `~/Library/LaunchAgents` owns macOS service definitions.
- `~/.hermes/scripts` owns machine-level deterministic wrappers and maintenance scripts.
- This repository owns reviewed local plugins, deployment helpers and durable documentation—not a fork of Hermes core.

The active Hermes checkout must match official `NousResearch/hermes-agent` `origin/main`: zero local commits and zero behavioral source diff. Old files under `patches/hermes-agent/` are historical migration artefacts only; the current updater does not apply or carry them.

Current local behavior is extension-first:

- **Linear:** the eleven-file profile-local plugin owns native AgentSession routing, durable channel-route reservation, lifecycle/closure policy, official-MCP outbound tools, retention classification, and secret-safe tool-driven ephemeral `thought` progress. Gateway heartbeats are not used as Linear execution progress. Fresh human mentions are scoped by the new AgentSession ID, so an earlier completed manager session cannot poison a distinct new session.
- **Honcho:** profile-local `honcho.json` plus the loopback-only `honcho.localhost` alias preserves process-scoped JWT authentication. Auth is never disabled and JWTs are never written to config.
- **Credential bootstrap:** the external Keychain → official 1Password SDK wrapper remains a separately owned launcher contract. Revalidate it after every core update; do not modify upstream core merely to recognize the wrapper.

Upgrade acceptance therefore has four independent gates:

1. Official core checkout equals `origin/main` and the updater backup exists.
2. Reviewed local plugin commit is on private `hermes-setup` `origin/main`, then commit-pinned into each profile.
3. Every restarted profile serves the expected plugin version and clean health/outbox state.
4. Real canaries pass: Linear fresh-session + ephemeral-progress + human-Done closure, and Honcho authenticated profile/card/search reads.

If a future upstream regression cannot be solved by config, plugin, wrapper or supported sidecar, isolate a candidate core change in a separate worktree with RED/GREEN tests and independent review. Do not place it into a managed release or production updater chain until that necessity is proven.

Quicksilver retirement is deliberately gated:

1. `components/memory/honcho-codex-bridge/scripts/quicksilver_soak_cleanup.py` is the byte-for-byte source copy of the deployed canonical soak cleanup. It removes the recovery checkout and managed candidate only after the scheduler reaches 8 completed runs and the persisted soak history contains seven successful calendar days with no failures.
2. The remaining Python canary, gateway/adapter candidates, Homebrew formula, and Homebrew-backup environments are **not** deleted by unattended automation. After the canonical completion marker exists, re-run live process, `lsof`, LaunchAgent, cron, config, global-CLI, and path-safety checks; then show the exact `brew uninstall` and removal commands for explicit approval. Do not infer approval from the earlier soak schedule.

The canonical cleanup remains general-profile no-agent job `c083e57807d7` at 04:40. A proposed destructive post-soak job was rejected during independent review and removed before its first run; no post-cleanup script remains deployed.

The npm audit remains advisory-only for the unresolved build surfaces. The installed TUI graph already resolves direct `eslint-plugin-react` to `7.37.5`; npm's remaining automatic remediation requires incompatible actions (React Router/`electron-builder` downgrades or ESLint major changes). Do not use `npm audit fix --force`. Re-evaluate when upstream publishes compatible patched releases, then require web/TUI/Desktop typecheck, test, and build gates before committing lockfile changes.
