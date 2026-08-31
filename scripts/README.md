# Root script ownership

`hermes-setup/scripts/` is reserved for fleet-wide utilities and explicit compatibility or recovery entrypoints. Component-owned implementations belong under `components/<domain>/<component>/scripts/`. A script is not moved or removed until its cron, LaunchAgent, runtime wrapper, Git configuration, CI, documentation and manual-runbook callers are classified and verified.

## Change contract

Any path, taxonomy, executable, service-label, config-key or credential-boundary change must carry a fleet impact manifest:

1. exact old → new coordinates;
2. all nine profiles' active cron `script` and full `prompt` fields;
3. profile-local wrappers, enabled skills/runbooks, LaunchAgents, process argv/cwd, CI/deploy manifests and documentation commands;
4. each hit classified as active caller, runtime compatibility, historical evidence, rollback or stale;
5. an owner, rollback coordinate and safe canary for every active caller;
6. the same resolved post-change inventory with zero active stale references.

Historical logs and outputs are evidence and are not rewritten to satisfy a search count. Reminder, destructive, credential-rotation and spending jobs are not bulk-fired: unaffected callers use syntax/import/read-only probes, while a touched path or contract requires a bounded real canary.

## Provisional inventory (OPS-204 baseline)

This table records the first caller pass. `candidate` is not deletion approval; it means further authoritative usage evidence is required.

| Script | Provisional ownership | Live/canonical evidence | Current decision |
|---|---|---|---|
| `backup-honcho.sh` | Compatibility wrapper | Delegates to deployed `~/.hermes/services/honcho-stack/backup-honcho.sh`; covered by backup tests | Keep as explicit compatibility entrypoint pending wrapper-consumer audit |
| `backup_ops.py` | Fleet backup primitive | Used by profile and Honcho backup/restore paths; covered by `test_backup_ops.py` | Keep at root unless backup ownership is consolidated under one operations component |
| `bot-tokens.env.example` | Retired credential migration evidence | Docs mark plaintext fan-out retired | Move with or retire alongside `setup-bots.sh`; never accept real values |
| `cleanup_honcho_workspaces.py` | One-shot destructive migration utility | Test-only caller; exact legacy inventory and confirmation string | Recovery/history candidate; do not run or delete without closure evidence |
| `config-snapshot.sh` | Fleet configuration snapshot | `ai.hermes.config-snapshot.plist` | Active root fleet utility |
| `derya-gh-credential.sh` | Derya GitHub broker transport | Active global Git credential helper | Move under GitHub broker/security component only with Git config compatibility mapping |
| `derya-gh-keychain.sh` | Derya interactive `gh` broker wrapper | No authoritative caller found in first pass | Candidate; verify shell aliases/runbooks before any retirement |
| `github_app_token_broker.py` | Derya GitHub App broker implementation | Called by credential wrappers and tested | Component-owned candidate; preserve managed runtime destination |
| `hermes-agent-ssh-guard.sh` | GitHub personal SSH guard | Installer/tests/component README | Component-owned candidate; preserve deployed transport guard contract |
| `hermes-gateway-keychain.sh` | Fleet gateway credential bootstrap | All nine gateway LaunchAgents | Active compatibility/runtime entrypoint; do not move without 9/9 launchd canary |
| `manage_hermes_agent_patches.py` | Hermes core patch lifecycle utility | Test-only caller in first pass | Candidate; verify upgrade runbook/manual usage |
| `notify-online.sh` | Fleet login notification | `ai.hermes.fleet-online.plist` and operations docs | Active root fleet utility |
| `profile-backup-quick.sh` | Fleet profile backup | `ai.hermes.backup-state.plist`, docs and tests | Active root fleet utility |
| `recover-marketing-state-approved.sh` | One-shot marketing state recovery | Fail-closed test contract only | Recovery/history candidate; preserve until recovery evidence is formally archived |
| `setup-bots.sh` | Retired plaintext fleet setup | Immediate fail-closed exit; docs mark retired | Retirement candidate with `bot-tokens.env.example`; preserve history until migration closure review |
| `watchdog.sh` | Fleet watchdog | `ai.hermes.watchdog.plist`, docs and live runtime | Active root fleet utility; OPS-202 owns delivery-storm repair |
| `wire-tinyfish.sh` | Fleet web-stack installer | Setup/operations docs; no scheduled caller | Manual installer candidate; validate against current native Hermes/TinyFish setup before keeping |

## Baseline evidence

- Tracked root files: 17.
- Generated `scripts/__pycache__` files found on disk: 11; they are not canonical source and require a separate ignored-artifact cleanup check.
- Shell syntax: 12/12 PASS.
- Python AST parse: 4/4 PASS.
- Targeted script tests: 59 PASS; the combined pass also exposed one unrelated existing taxonomy-test failure because `operations/gateway-restart-request` is absent from the expected component set.
- Fleet cron impact scan: 27/27 enabled jobs report `last_status=ok`; 17/17 configured script targets exist; active references to the six renamed `integrations/*` coordinates are zero.
- LaunchAgent impact scan: all inspected Hermes program entrypoints exist; renamed `integrations/*` coordinates are absent.

No root script was moved or deleted in this baseline pass.
