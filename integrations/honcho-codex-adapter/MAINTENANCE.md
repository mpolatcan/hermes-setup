# Honcho Codex Adapter Maintenance

This runbook is the operator reference for the studio deployment. It covers the
adapter and the narrow Honcho changes that depend on it. It does not replace the
upstream Honcho or Hermes documentation.

## Production contract

| Surface | Honcho model | Route |
|---|---|---|
| Deriver | `honcho-deriver-luna` | Codex adapter |
| Summary | `honcho-summary-luna` | Codex adapter |
| Dialectic `minimal` through `max` | `honcho-dialectic-luna` | Codex adapter |
| Dream deduction | `honcho-dream-deduction-luna` | Codex adapter |
| Dream induction | `honcho-dream-induction-luna` | Codex adapter |
| Embeddings | `openai/text-embedding-3-small` | OpenRouter, not this adapter |

The nine text-generation surfaces use six of the seven advertised aliases in the
current Honcho configuration. `honcho-dialectic-minimal-luna` and the direct
`gpt-5.6-luna` alias remain valid adapter endpoints but are not selected by the
current Honcho `config.toml`.

Runtime invariants:

- listener: `127.0.0.1:18080`; never expose it on a public interface;
- Honcho container URL: `http://host.docker.internal:18080/v1`;
- upstream allowlist: `gpt-5.6-luna` only;
- active upstream concurrency: one request;
- bounded admission: eight active-plus-waiting requests by default;
- weighted queue: Dialectic 8, Summary 4, Deriver 2, Dream 1, with a 30-second
  aging override;
- upstream timeout: 90 seconds; automatic SDK retries: zero;
- caller-visible output is capped in the application with `o200k_base`;
- plain text may end with `finish_reason=length`; JSON and tool calls fail closed
  with `output_limit_exceeded` rather than being truncated;
- Dream completion guards advance only when deduction and induction both succeed;
- no paid OpenRouter fallback exists for text generation.

## Deployment files

| Purpose | Path |
|---|---|
| Tracked adapter | `/Users/mutlupolatcan/Desktop/hermes-setup/integrations/honcho-codex-adapter` |
| Non-secret adapter config | `config/adapter.toml` |
| Hermes compatibility manifest | `compatibility/hermes-current.json` |
| Honcho checkout | `/Users/mutlupolatcan/honcho-stack/server` |
| Credential-resolving launcher | `/Users/mutlupolatcan/.hermes/scripts/honcho-codex-adapter-keychain.sh` |
| Tracked launcher source | `scripts/honcho_codex_adapter_launcher.sh` |
| Production runtime | `/Users/mutlupolatcan/.hermes/runtime/honcho-codex-adapter` |
| Runtime deployment | `scripts/deploy_runtime.sh` |
| 1Password SDK resolver | `scripts/resolve_onepassword_secret.py` |
| 1Password SDK venv | `onepassword-sdk-venv` under the production runtime (Python 3.13) |
| 1Password SDK lock | `requirements/onepassword-sdk.txt` |
| Adapter runtime constraints | `requirements/runtime-constraints.txt` |
| LaunchAgent | `/Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.honcho-codex-adapter.plist` |
| Standard log | `/Users/mutlupolatcan/.hermes/logs/honcho-codex-adapter.log` |
| Error log | `/Users/mutlupolatcan/.hermes/logs/honcho-codex-adapter.error.log` |
| Soak wrapper | `/Users/mutlupolatcan/.hermes/profiles/general/scripts/honcho_codex_soak_cron.sh` |
| Soak history | `/Users/mutlupolatcan/.hermes/profiles/general/metrics/honcho-codex-soak.jsonl` |

Credentials are resolved at runtime from macOS Keychain and 1Password. Never copy
the bearer value into this repository, a command line, logs, chat, or a rollback
snapshot. The launcher may contain a 1Password item URI; the secret value must not.
The production and soak paths must use the official 1Password SDK and must never
fall back to the `op` executable. SDK failure is a fail-closed startup failure.

## Runtime deployment and 1Password SDK bootstrap

The adapter remains on the Hermes-selected Python 3.14 runtime. The 1Password SDK
0.4.0 publishes macOS arm64 wheels only through CPython 3.13, so secret resolution
uses a separate, narrow Python 3.13 venv. Production artifacts live outside Desktop
to avoid macOS Desktop Folder TCC prompts in the LaunchAgent execution context.

```bash
brew install python@3.13
cd /Users/mutlupolatcan/Desktop/hermes-setup/integrations/honcho-codex-adapter
./scripts/deploy_runtime.sh
bash -n /Users/mutlupolatcan/.hermes/scripts/honcho-codex-adapter-keychain.sh
```

The deploy script copies source into `/Users/mutlupolatcan/.hermes/runtime`, builds
pinned Python 3.14 and Python 3.13 venvs, validates config and Hermes compatibility,
and installs the tracked launcher with mode `0700`. It does not restart the service.
Restart only after explicit operator approval:

```bash
launchctl kickstart -k gui/$(id -u)/ai.hermes.honcho-codex-adapter
```

Do not add the service-account token or resolved bearer to a venv, repository,
plist, or shell history. The launcher reads the bootstrap token from macOS Keychain,
passes it only to the resolver process, and removes it before starting the adapter.
The resolver writes only the requested secret to stdout for immediate capture; all
error messages are sanitized and resolution has a bounded timeout.

## Routine health check

Run the non-secret checks first:

```bash
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:8000/health
launchctl print gui/$(id -u)/ai.hermes.honcho-codex-adapter
lsof -nP -iTCP:18080 -sTCP:LISTEN
```

Expected results are `{"status":"ok"}` from both HTTP checks, one listener on
`127.0.0.1:18080`, and a running LaunchAgent. The adapter health endpoint proves
process liveness only; use the deterministic probe for authenticated model and
contract coverage:

```bash
cd /Users/mutlupolatcan/Desktop/hermes-setup/integrations/honcho-codex-adapter
.venv/bin/python scripts/honcho_codex_soak_probe.py --json
```

The probe resolves its bearer through Keychain and 1Password without printing it.
It checks both health endpoints, the exact seven-model catalog, Summary, structured
Deriver output, Dialectic text/tool behavior, and both Dream tool envelopes.

## Test and release gate

From the adapter directory:

```bash
HERMES_PY=/absolute/path/to/selected/hermes/python
HERMES_SITE="$($HERMES_PY -c 'import site; print(site.getsitepackages()[0])')"
PYTHONPATH="src:$HERMES_SITE" .venv/bin/python -m honcho_codex_adapter.cli \
  --config config/adapter.toml --check-config
PYTHONPATH="src:$HERMES_SITE" .venv/bin/python scripts/check_hermes_compat.py --json
PYTHONPATH="src:$HERMES_SITE" .venv/bin/python -m unittest discover -s tests -v
PYTHONPATH="src:$HERMES_SITE" .venv/bin/python -m ruff check src tests scripts
PYTHONPATH="src:$HERMES_SITE" .venv/bin/python -m compileall -q src scripts tests
bash -n scripts/honcho_codex_soak_cron.sh
bash -n scripts/run_honcho_guard_tests.sh
```

The selected site-packages path is discovered from its Python executable; no versioned
Cellar path is tracked. Private imports are confined to `compat.py` and must match the
reviewed signature manifest before behavioral tests run.

Then validate the Honcho completion guard in its checkout:

```bash
cd /Users/mutlupolatcan/Desktop/hermes-setup/integrations/honcho-codex-adapter
scripts/run_honcho_guard_tests.sh
cd /Users/mutlupolatcan/honcho-stack/server
.venv/bin/python -m ruff check \
  src/dreamer/orchestrator.py tests/dreamer/test_dreamer_integration.py
```

The helper mounts the Honcho checkout read-only, creates a private Docker network and
temporary pgvector container with no published ports, installs test-only packages in
a disposable venv, runs the 11 guard integration tests, and removes the database and
network on exit. It intentionally does not use production database credentials or
production data. It requires one running Honcho API container only to reuse the exact
deployed image.

Canary roles are deliberately separate:

- `honcho_dream_tool_canary.py`: all ten Dream tool envelopes; no tool execution;
- `honcho_dream_effect_canary.py`: real writes in a uniquely prefixed disposable
  workspace, including failed-write and idempotency checks, followed by zero-residue
  cleanup;
- `honcho_dream_e2e_canary.py`: real `run_dream` orchestration; both specialists
  must succeed before the result passes;
- `honcho_codex_soak_probe.py`: deterministic daily coverage, not a substitute for
  the mutation or full orchestration gates.

Run mutation and E2E canaries inside the production API container so they use the
real Postgres, Redis, mounted config, and container credentials. A host-side E2E can
fail with local PostgreSQL authentication even while the production route is healthy.
Always require the canary's cleanup counts to be zero before accepting the run.

## Lifecycle

The adapter is managed by `ai.hermes.honcho-codex-adapter`. A restart is a production
change: inspect the diff and obtain explicit operator approval before running it.
The approved command is:

```bash
launchctl kickstart -k gui/$(id -u)/ai.hermes.honcho-codex-adapter
```

After a restart, repeat the routine health check and deterministic probe. If Honcho
model routing changed, recreate only the affected `api` and/or `deriver` Compose
services after separate approval, then verify both containers and inspect recent
logs for authentication, timeout, queue, or tool-contract errors.

## Seven-day soak

The initial production gate is a no-agent Hermes cron job:

- job: `Honcho Codex 7-day soak`;
- job ID: `7784a37d506f`;
- schedule: `20 4 * * *` (04:20 TRT), seven runs;
- history: `honcho-codex-soak.jsonl` at the path above;
- delivery: Telegram on failures and on the final aggregate report.

The 04:20 slot follows the 04:00 Honcho database backup. Do not move it onto the
backup window. The soak is complete only after all seven persisted records and the
final delivery are verified; scheduling it is not completion evidence.

## Hermes upgrade gate

Follow [the illustrated upgrade lifecycle](docs/upgrade-lifecycle.md). The automated
non-production gate is:

```bash
scripts/stage_hermes_upgrade.sh /absolute/path/to/candidate-python config/adapter.toml
```

It validates the typed config, exact eight-symbol compatibility manifest, the complete adapter
and contract suite, and Python compilation without editing the production launcher.
Authenticated `:18081` probes, Dream effect/E2E canaries, the 11 Honcho guard tests,
production promotion, and restart remain separate approval gates.

If the private transport contract breaks, keep the OpenAI-compatible HTTP façade and
replace only `backends/hermes.py` with the official Codex App Server backend. Do not
weaken schema, tool, authentication, output-limit, cancellation, or loopback controls
to make an upgrade pass.

## Rollback

Rollback snapshots such as `config.toml.pre-*` and `docker-compose.yml.pre-*` are
local operational artifacts. Do not commit them. Before a routing change, create a
dated snapshot and verify it byte-for-byte. To roll back:

1. Stop new canaries and preserve the failing logs and soak record.
2. Show the exact restore diff and Compose/LaunchAgent commands; obtain approval.
3. Restore the last verified Honcho configuration snapshot.
4. Recreate only affected Honcho services and restart the adapter only if its code or
   launcher changed.
5. Verify both health endpoints, the listener count, model routes, and recent logs.
6. Run the deterministic probe and the full Dream E2E canary.

Do not mark a rollback complete if only the liveness endpoints pass.

## Troubleshooting order

1. Confirm the LaunchAgent and the single loopback listener.
2. Check adapter and Honcho health endpoints.
3. Inspect adapter error logs without printing credentials.
4. Run the deterministic probe and identify the first failing surface.
5. For `401`, verify Keychain/1Password resolution and Codex OAuth state; never paste
   tokens into diagnostics.
6. For `503 queue_full`, inspect request load and queue aging before increasing the
   bounded capacity.
7. For `504 timeout_error`, verify the 90-second override and upstream behavior;
   retries must remain zero unless a new bounded policy is explicitly approved.
8. For `output_limit_exceeded`, treat structured output as failed; never truncate and
   execute partial JSON or tool arguments.
9. For Dream failures, distinguish envelope, persisted-effect, and full-orchestration
   failures. Completion guards must remain unchanged after either specialist fails.
10. Re-check host/container boundaries before blaming production for a host-only
    database authentication error.