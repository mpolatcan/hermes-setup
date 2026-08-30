# Rollback runbook

This runbook restores either the adapter code or Honcho routing without exposing the local bearer credential. Run it from the named working directory. Do not use a bare `docker compose up`: without vault injection it resolves `HONCHO_CODEX_ADAPTER_API_KEY` to an empty value.

Any `launchctl` restart or production config replacement requires explicit operator approval immediately before execution.

## Preflight

```bash
cd /Users/mutlupolatcan/.hermes/source/hermes-setup/components/memory/honcho-codex-bridge
test -z "$(git status --porcelain)" || {
  echo "Refusing rollback with a dirty adapter worktree" >&2
  exit 1
}
git tag --list 'adapter-known-good-*'
```

Before a new rollout, create a stage-specific annotated tag:

```bash
git tag -a "adapter-known-good-YYYY-MM-DD-stage" \
  -m "Known-good adapter state before STAGE"
```

## Restore adapter code

Set `KNOWN_GOOD` to a reviewed annotated tag. The restore is committed instead of rewriting history.

```bash
cd /Users/mutlupolatcan/.hermes/source/hermes-setup/components/memory/honcho-codex-bridge
KNOWN_GOOD=honcho-codex-adapter-known-good-2026-07-19
git cat-file -e "${KNOWN_GOOD}^{commit}"
git restore --source="$KNOWN_GOOD" --staged --worktree -- .
git diff --cached --check
git commit -m "revert: restore adapter to $KNOWN_GOOD"
```

If the restore produces no diff, do not create an empty commit.

After explicit approval, restart the adapter LaunchAgent:

```bash
/bin/launchctl kickstart -k \
  gui/$(id -u)/ai.hermes.honcho-codex-adapter
```

## Restore Honcho routing

Canonical live config:

```text
/Users/mutlupolatcan/.hermes/services/honcho-stack/server/config.toml
```

Available stage snapshots, newest scope first:

| Snapshot | Restored scope |
|---|---|
| `config.toml.pre-dialectic-all-levels-codex-20260719` | Keep earlier routes; undo all-level dialectic rollout |
| `config.toml.pre-dialectic-low-codex-canary` | Keep summary/deriver; undo dialectic-low and later rollout |
| `config.toml.pre-deriver-codex-canary` | Keep summary; undo deriver and later rollout |
| `config.toml.pre-summary-codex-canary` | Keep the first canary only; undo summary and later rollout |
| `config.toml.pre-codex-canary` | Full pre-adapter routing |

Choose one reviewed snapshot and preserve the current file before replacement:

```bash
cd /Users/mutlupolatcan/.hermes/services/honcho-stack/server
SNAPSHOT=config.toml.pre-codex-canary
CONFIG=config.toml
cp -p "$CONFIG" "$CONFIG.pre-rollback-$(date +%Y%m%d-%H%M%S)"
install -m 0644 "$SNAPSHOT" "$CONFIG"
```

Inject the existing bearer value directly from 1Password. The value remains process-local and is never written to a file or printed:

```bash
token=$(/usr/bin/security find-generic-password \
  -s com.polatcangames.hermes.op-service-account \
  -a general -w)
secret=$(OP_SERVICE_ACCOUNT_TOKEN="$token" /opt/homebrew/bin/op read \
  'op://xaegpgrxyvqpb7dkrmlxpj2xbe/oe3kgxx7h6fb4axvnnejyw4aoa/jthjqdc63c6svaaiisuz3f3iwm')
unset token
export HONCHO_CODEX_ADAPTER_API_KEY="$secret"
unset secret
docker compose config --quiet
docker compose up -d --force-recreate api deriver
unset HONCHO_CODEX_ADAPTER_API_KEY
```

## Verification

Adapter readiness and listener:

```bash
curl -fsS http://127.0.0.1:18080/healthz
/usr/sbin/lsof -nP -iTCP:18080 -sTCP:LISTEN
```

Honcho services:

```bash
curl -fsS http://127.0.0.1:8000/health
cd /Users/mutlupolatcan/.hermes/services/honcho-stack/server
docker compose ps api deriver
```

For an adapter-backed route, exercise non-stream, SSE, and a real tool loop; the script creates and removes its own isolated workspace:

```bash
cd /Users/mutlupolatcan/.hermes/source/hermes-setup/components/memory/honcho-codex-bridge
python3 scripts/honcho_low_canary.py
```

Expected terminal evidence includes both `"status": "pass"` and `"cleanup_absent": true`.

Finally inspect only post-restart logs for fatal signals. Do not mistake normal Uvicorn logger names such as `uvicorn.error` for failures.

## Abort conditions

Stop and restore the immediately preceding known-good state if any of these occur:

- adapter health or listener check fails;
- API or deriver fails to become healthy;
- Compose warns that `HONCHO_CODEX_ADAPTER_API_KEY` is unset;
- canary lacks its required tool call, stream response, or cleanup confirmation;
- new traceback, authentication failure, queue saturation, or repeated timeout appears.
