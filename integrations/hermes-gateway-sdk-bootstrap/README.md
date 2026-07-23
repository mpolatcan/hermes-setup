# Hermes Gateway SDK Bootstrap

Fail-closed 1Password SDK bootstrap for Hermes Telegram gateways on the macOS fleet.

## Why this exists

Hermes 0.19.0's built-in 1Password source calls the `op` CLI before applying environment precedence. Supplying already-resolved environment variables with `override_existing: false` therefore does **not** prevent `op read` child processes. That path is unsuitable for unattended launchd gateways on this host.

This component resolves the configured `ENV_VAR -> op://...` map through the official Python SDK before starting Hermes. The built-in Hermes 1Password source must be disabled for every promoted profile.

## Security contract

- The service-account token is obtained by the existing Keychain launcher.
- The bootstrap authenticates one SDK client and resolves every configured reference.
- Startup fails closed if the token, config, mapping, SDK call, or any value is missing.
- Secret values and references are never logged.
- The bootstrap token is removed before Hermes is executed.
- Secrets exist only in the gateway process environment.
- In strict/promoted mode, `secrets.onepassword.enabled` must be exactly `false`; otherwise startup is rejected.
- The temporary rollout launcher may pass one explicit absolute legacy executable; only profiles still carrying `enabled: true` are dispatched there, without SDK resolution.
- Hermes starts through the supported `python -m hermes_cli.main` boundary.

## Runtime layout

Candidate:

```text
~/.hermes/runtime/hermes-gateway-sdk-bootstrap-candidate/
```

Production:

```text
~/.hermes/runtime/hermes-gateway-sdk-bootstrap/
```

The production launcher is:

```text
~/.hermes/scripts/hermes-gateway-keychain.sh
```

The canonical fleet restart helper is:

```text
~/.hermes/shared-skills/canonical/fleet-lifecycle-ops/scripts/restart_fleet_notify.sh
```

## Test

```bash
HERMES_PY=~/.hermes/runtime/hermes-agent/venv/bin/python
"$HERMES_PY" -m unittest discover \
  -s integrations/hermes-gateway-sdk-bootstrap/tests \
  -p 'test_*.py' -v
```

The tests cover disabled-provider enforcement, reference validation, one-client resolution, fail-closed behavior, timeout handling, sanitized errors, token removal, check-only output, and the final Hermes exec boundary.

## Install a candidate

```bash
bash integrations/hermes-gateway-sdk-bootstrap/scripts/install_candidate.sh
```

To install into a different isolated target:

```bash
bash integrations/hermes-gateway-sdk-bootstrap/scripts/install_candidate.sh /absolute/target
```

The installer requires Homebrew Python 3.13 and `uv`, installs fully pinned dependencies, runs `uv pip check`, copies the bootstrap with mode `0700`, and verifies imports.

## Pre-production canary

1. Copy each profile config to a mode-`0600` temporary directory.
2. Set only `secrets.onepassword.enabled: false` in each temporary copy.
3. Obtain the profile service-account token from macOS Keychain without printing it.
4. Run the candidate with `--config <temporary-copy> --check-only`.
5. Confirm all expected environment names resolve, `token_removed` is true, and no values/references appear in output.
6. Compare `op read` process PIDs before and after. The delta must be empty.
7. Confirm live gateway PIDs are unchanged.

## Transition-safe dispatch

During rollout, the launcher passes an explicit legacy Hermes executable:

```text
--legacy-hermes /opt/homebrew/bin/hermes
```

The profile's single config state selects the path:

- `secrets.onepassword.enabled: true` -> legacy Hermes with the bootstrap token retained for the existing built-in provider.
- `secrets.onepassword.enabled: false` -> SDK resolution followed by Quicksilver, with the bootstrap token removed.

This makes launcher installation safe before any profile config is changed and permits profile-by-profile migration. After all nine profiles are migrated and verified, remove the legacy argument so the launcher becomes strict and any accidental re-enable fails closed.

## Production rollout gate

Do not perform these changes without explicit approval.

1. Back up all nine profile configs and the live launcher.
2. Install the exact tested artifact into the production runtime path.
3. Install the transition launcher at `~/.hermes/scripts/hermes-gateway-keychain.sh` with mode `0700`. Profiles still carrying `enabled: true` remain on legacy Hermes.
4. For one auxiliary profile at a time, atomically change only `secrets.onepassword.enabled` from `true` to `false`, then restart it with the canonical supervisor-safe helper.
5. Verify launchd state, PID, Quicksilver process command, Telegram readiness, and zero new `op read` children before advancing.
6. Continue through the remaining auxiliary profiles.
7. Migrate and restart `general` last through Telegram `/restart` when available.
8. Remove the launcher's `--legacy-hermes` argument, install the strict launcher, and confirm all nine configs remain disabled.

## Rollback

If any profile fails:

1. Restore its previous config and launcher snapshot.
2. Restart only that profile with the canonical restart helper.
3. Verify the previous Hermes process command and Telegram readiness.
4. Leave remaining profiles unchanged until the failure is understood.

Do not replace launchd plists with `hermes gateway start`; the fleet intentionally uses a custom Keychain bootstrap launcher, and the Hermes CLI may report that service definition as stale.
