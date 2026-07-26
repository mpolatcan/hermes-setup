# Hermes Gateway SDK Bootstrap

Fail-closed 1Password SDK bootstrap for Hermes Telegram gateways on the macOS fleet.

## Why this exists

Hermes 0.19.0's built-in 1Password source calls the `op` CLI before applying environment precedence. Supplying already-resolved environment variables with `override_existing: false` therefore does **not** prevent `op read` child processes. That path is unsuitable for unattended launchd gateways on this host.

This component resolves the configured `ENV_VAR -> op://...` map through the official Python SDK before starting Hermes. The built-in Hermes 1Password source must be disabled for every promoted profile.

## Security contract

- The service-account token is obtained by the existing Keychain launcher.
- The bootstrap authenticates one SDK client and resolves the sorted reference set with one native `secrets.resolve_all(...)` batch call. It does not issue one vault request per environment variable.
- Startup fails closed if the token, config, mapping, SDK batch call, any individual response, or any value is missing.
- Secret values and references are never logged.
- The service-account token exists briefly in the mode-`0700` Keychain wrapper and bootstrap process environment, then is removed before Hermes is executed.
- Resolved values exist briefly in bootstrap memory and only the selected target child environment. Gateway mode resolves the full configured map; general-only `serve` mode resolves the same map and binds a fixed Desktop backend to `127.0.0.1:9120`; general-only `desktop` mode resolves only `HERMES_DASHBOARD_SESSION_TOKEN`, renames it to the Desktop remote-token variable, fixes the URL to `http://127.0.0.1:9120`, and never gives Desktop the bootstrap token, Honcho root/JWT, or unrelated credentials; maintenance `send` is restricted to the configured Telegram home target, resolves only `TELEGRAM_*` references, rejects local-file/media delivery, and builds the bootstrap and child from small non-secret environment allowlists rather than inheriting the caller environment.
- In strict/promoted mode, `secrets.onepassword.enabled` must be exactly `false`; otherwise startup is rejected.
- The temporary rollout launcher may pass one explicit absolute legacy executable; only profiles still carrying `enabled: true` are dispatched there, without SDK resolution.
- Hermes starts through the managed `v0.19.0` console executable.

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

Install the narrow maintenance-send wrapper separately with mode `0700`:

```bash
/usr/bin/install -m 700 \
  integrations/hermes-gateway-sdk-bootstrap/scripts/hermes_send_keychain.sh \
  /Users/mutlupolatcan/.hermes/scripts/hermes-send-keychain.sh
```

Install the general-only loopback Desktop backend wrapper and LaunchAgent with modes `0700` and `0600`:

```bash
/usr/bin/install -m 700 \
  integrations/hermes-gateway-sdk-bootstrap/scripts/hermes_serve_keychain.sh \
  /Users/mutlupolatcan/.hermes/scripts/hermes-serve-keychain.sh
/usr/bin/install -m 600 \
  integrations/hermes-gateway-sdk-bootstrap/launchd/ai.hermes.serve-general.plist \
  /Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.serve-general.plist
launchctl bootstrap gui/$(id -u) \
  /Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.serve-general.plist
```

Install the General-only secure Desktop launcher and LaunchAgent with the same modes:

```bash
/usr/bin/install -m 700 \
  integrations/hermes-gateway-sdk-bootstrap/scripts/hermes_desktop_keychain.sh \
  /Users/mutlupolatcan/.hermes/scripts/hermes-desktop-keychain.sh
/usr/bin/install -m 600 \
  integrations/hermes-gateway-sdk-bootstrap/launchd/ai.hermes.desktop-general.plist \
  /Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.desktop-general.plist
launchctl bootstrap gui/$(id -u) \
  /Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.desktop-general.plist
```

To install into a different isolated target:

```bash
bash integrations/hermes-gateway-sdk-bootstrap/scripts/install_candidate.sh /absolute/target
```

The installer requires the signed, notarized Python.org 3.13 runtime at `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13` and `uv`, installs fully pinned dependencies, runs `uv pip check`, copies the bootstrap with mode `0700`, and verifies imports. Override `PYTHON` only for an explicit canary; production must not fall back to an ad-hoc Homebrew interpreter.

## Pre-production canary

1. Copy each profile config to a mode-`0600` temporary directory.
2. Set only `secrets.onepassword.enabled: false` in each temporary copy.
3. Obtain the profile service-account token from macOS Keychain without printing it.
4. Run the candidate with `--config <temporary-copy> --check-only`.
5. Confirm all expected environment names resolve, `token_removed` is true, and no values/references appear in output.
6. Compare `op read` process PIDs before and after. The delta must be empty.
7. Confirm live gateway PIDs are unchanged.

## Production dispatch (completed)

Production is strict across all nine profiles:

- `secrets.onepassword.enabled: false` in every profile.
- `~/.hermes/scripts/hermes-gateway-keychain.sh` resolves configured references through the 1Password SDK and execs the managed Hermes `v0.19.0` runtime.
- The production launcher carries no `--legacy-hermes` argument. An accidental re-enable therefore fails closed.
- `~/.hermes/scripts/hermes-send-keychain.sh` re-execs itself with a clean environment before Keychain lookup, then uses the same resolution boundary for the narrow Telegram-home `hermes send` command used by maintenance notifications. The bootstrap token is removed before Hermes executes.
- `~/.hermes/scripts/hermes-serve-keychain.sh` does the same for a general-only, argument-free `hermes serve --isolated --host 127.0.0.1 --port 9120` backend used by Hermes Desktop remote-backend mode.
- `~/.hermes/scripts/hermes-desktop-keychain.sh` launches Hermes Desktop with only the fixed loopback URL and its dedicated session token. The full profile credential map remains confined to the serve backend.
- Homebrew Hermes remains an inert rollback package during the soak period; no production launcher, profile init file, or maintenance script depends on it.

The historical transition path remains implemented and tested for rollback analysis, but it is not the production dispatch path.

## Production verification gate

After any bootstrap or Hermes runtime change:

1. Back up all nine profile configs and live launchers.
2. Run the bootstrap test suite and syntax checks.
3. Query the official service-account rate-limit state before a fleet rollout. If account-level `remaining` is zero, keep the affected LaunchAgents unloaded until the reported reset window expires; do not let `KeepAlive` create a retry storm.
4. Restart auxiliary profiles serially, leaving at least 15 seconds between profiles. Require a stable PID and the final scoped credential boundary before continuing.
5. Restart `general` last through Telegram `/restart` when available.
6. Verify launchd PID replacement, the bootstrap runtime's exact `sys.base_prefix`, managed-first `PATH`, Telegram readiness, and zero persistent `op read` children. Do not hard-code the intended Python version as proof of the live process.
7. Verify `hermes-send-keychain.sh` against a non-general profile before relying on maintenance alerts.
8. Verify `ai.hermes.serve-general` on `127.0.0.1:9120`, then verify the Desktop process has only the remote URL/session token and a tool-level `honcho_context` call succeeds.

## Rollback

If any profile fails:

1. Restore its previous config and launcher snapshot.
2. Restart only that profile with the canonical restart helper.
3. Verify the previous Hermes process command and Telegram readiness.
4. Leave remaining profiles unchanged until the failure is understood.

Do not replace launchd plists with `hermes gateway start`; the fleet intentionally uses a custom Keychain bootstrap launcher, and the Hermes CLI may report that service definition as stale.
