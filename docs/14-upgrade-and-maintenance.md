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

Authenticated self-hosted Honcho instances on loopback/LAN need a separate explicit opt-in because upstream otherwise replaces every environment-owned API key with the unauthenticated `"local"` placeholder. General sets `hosts.hermes_general.authRequired=true`; the scoped JWT remains process-only. The verified compatibility patch is:

```text
patches/hermes-agent/0002-fix-honcho-authenticated-local-env-credentials.patch
```

The fleet's macOS LaunchAgents intentionally use a Keychain → 1Password SDK bootstrap wrapper instead of Hermes' generated direct-Python `ProgramArguments`. Exact generated-plist comparison therefore reports a false stale definition and `gateway start` can overwrite the credential boundary. The owned compatibility patch adds an explicit, fail-closed plist opt-out:

```text
patches/hermes-agent/0003-fix-gateway-externally-managed-launchd-definitions.patch
```

Only a native plist boolean `HermesManagedExternally=true` together with an exact `Label == get_launchd_label()` match opts out. Valid XML and binary plist encodings are supported by macOS and preserve the same typed contract. Missing/wrong labels, missing/false/string-valued markers, typed-malformed XML, and invalid binary/non-plist bytes remain on the strict comparison/repair path without crashing status/install/start. With a valid marker, normal status/install/start paths report the definition as externally managed and do not rewrite it; an explicit forced install remains the operator-controlled escape hatch. Before adding the marker to a live plist, preserve its SHA-256 and rollback copy, verify the wrapper's fail-closed credential bootstrap, and use a separate plist/reload/restart approval gate.

Hermes' shared send target parser recognizes platform-specific numeric and structured IDs but otherwise treats a target as a friendly channel name. Linear Agent Session UUIDs therefore fell through to directory/home resolution. The owned core patch adds a narrowly scoped Linear UUID target class, rejects malformed and cross-platform UUID-shaped aliases, and prevents cron from re-resolving already-explicit targets:

```text
patches/hermes-agent/0004-fix-send-preserve-opaque-uuid-delivery-targets.patch
```

Run the lightweight send target parser tests, cron delivery-target tests, and the complete send-message tool and CLI send suites before candidate promotion. A source-only acceptance must prove the exact UUID reaches the registered standalone sender without directory or home fallback; production acceptance additionally requires an approved non-stale Agent Session target and authoritative activity read-back.

Periodic gateway heartbeats are transient UI, but append-only control-plane adapters map every `send()` to a terminal activity. A three-minute Linear canary proved the generic heartbeat fallback could therefore complete an active AgentSession with `Working — …` before the real final response. The owned core patch capability-gates heartbeat edit/send calls on `SUPPORTS_MESSAGE_EDITING` and preserves prior message IDs after failed/partial sends:

```text
patches/hermes-agent/0005-fix-gateway-suppress-heartbeats-on-noneditable-platforms.patch
```

Before promotion, run `tests/gateway/test_long_running_notifications.py`, Ruff, compile checks, and an independent diff review. Production acceptance requires a fresh human-triggered AgentSession that lasts beyond the configured heartbeat interval, remains active without a heartbeat `response`, then completes with exactly one real terminal `response`; pending/in-flight/dead outbox counts must remain zero.

Linear later gained a vendor-native transient surface that does not share the terminal semantics of a normal append-only `send()`: ephemeral `thought` activities are replaced by the next activity. The follow-up compatibility patch preserves the append-only suppression by default while allowing adapters that explicitly declare `SUPPORTS_TRANSIENT_PROGRESS` to receive gateway heartbeats with typed metadata:

```text
patches/hermes-agent/0006-verified-feat-gateway-support-transient-progress-act.patch
```

The Linear adapter maps per-turn-keyed heartbeats to durable ephemeral `thought` activities; ordinary sends remain final `response` activities and generic tool-progress remains disabled. Acceptance must prove the first transient activity arrives after the configured interval, an identical heartbeat in a later follow-up gets a distinct durable activity, progress does not complete the AgentSession, final delivery replaces the ephemeral status, and no outbox row is pending, in-flight, or dead.

A later semantic-progress patch keeps a per-turn, lock-protected summary of the last safe tool phase so transport states such as `receiving stream response` cannot erase useful context:

```text
patches/hermes-agent/0007-verified-feat-gateway-publish-meaningful-heartbeat-c.patch
```

The summary layer is allowlist-only: it emits fixed phrases such as `Running tests` or `Reading Linear work state`, never raw commands, paths, issue identifiers, arbitrary tool names, tool results, or model reasoning. Parallel and completion-only events degrade conservatively to aggregate wording. Run both `tests/gateway/test_meaningful_heartbeat_progress.py` and `tests/gateway/test_long_running_notifications.py`; verify callback wiring with long-running notifications as the sole progress consumer, then require a real Linear AgentSession canary whose ephemeral `thought` shows the safe phase before the iteration counter and remains non-terminal.

After each Hermes Agent upgrade, classify all owned patches before promotion:

```bash
python3 scripts/manage_hermes_agent_patches.py \
  --runtime-root /absolute/path/to/candidate-hermes-agent \
  --mode check
```

Only `already-applied` and `upstreamed` pass. Use explicit `--mode apply` only against the side-by-side candidate; the manager rejects dirty worktrees and reports `applicable` versus `conflict` separately. `integrations/honcho-codex-adapter/scripts/stage_hermes_upgrade.sh` repeats this check fail-closed. For the local-auth patch, run the focused three local-auth tests and the full `tests/honcho_plugin/test_client.py` suite, then restart affected processes and require a real authenticated Honcho canary. For the externally-managed launchd patch, run the focused marker/rewrite/status regressions plus the complete gateway service and status suites; then validate one general-only marked plist canary without touching the other eight LaunchAgents. Roll back by restoring the previous immutable runtime pointer and the exact pre-marker plist copy, then restart only the approved profile and repeat PID, health, platform, and secret-boundary acceptance. Never replace the Honcho flag with a literal `apiKey` in profile files.

Quicksilver retirement is deliberately gated:

1. `integrations/honcho-codex-adapter/scripts/quicksilver_soak_cleanup.py` is the byte-for-byte source copy of the deployed canonical soak cleanup. It removes the recovery checkout and managed candidate only after the scheduler reaches 8 completed runs and the persisted soak history contains seven successful calendar days with no failures.
2. The remaining Python canary, gateway/adapter candidates, Homebrew formula, and Homebrew-backup environments are **not** deleted by unattended automation. After the canonical completion marker exists, re-run live process, `lsof`, LaunchAgent, cron, config, global-CLI, and path-safety checks; then show the exact `brew uninstall` and removal commands for explicit approval. Do not infer approval from the earlier soak schedule.

The canonical cleanup remains general-profile no-agent job `c083e57807d7` at 04:40. A proposed destructive post-soak job was rejected during independent review and removed before its first run; no post-cleanup script remains deployed.

The npm audit remains advisory-only for the unresolved build surfaces. The installed TUI graph already resolves direct `eslint-plugin-react` to `7.37.5`; npm's remaining automatic remediation requires incompatible actions (React Router/`electron-builder` downgrades or ESLint major changes). Do not use `npm audit fix --force`. Re-evaluate when upstream publishes compatible patched releases, then require web/TUI/Desktop typecheck, test, and build gates before committing lockfile changes.
