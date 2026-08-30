#!/bin/bash
# Start Hermes Desktop against the general-profile loopback backend through the fail-closed 1Password SDK bootstrap.
set -euo pipefail

[[ "$#" -eq 0 ]] || { printf >&2 "hermes-desktop-keychain accepts no arguments\n"; exit 64; }

exec /usr/bin/env -i \
  HOME="/Users/mutlupolatcan" USER="mutlupolatcan" LOGNAME="mutlupolatcan" \
  LANG="${LANG:-en_US.UTF-8}" \
  PATH="/Users/mutlupolatcan/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  /bin/bash --noprofile --norc -c '
    set -euo pipefail
    umask 077

    profile="general"
    for _ in {1..240}; do
      /usr/bin/pgrep -x Hermes >/dev/null 2>&1 || break
      /bin/sleep 0.25
    done
    /usr/bin/pgrep -x Hermes >/dev/null 2>&1 && {
      printf >&2 "Existing Hermes Desktop did not exit before secure launch\n"
      exit 67
    }

    service="com.polatcangames.hermes.op-service-account"
    token=$(/usr/bin/security find-generic-password -s "$service" -a "$profile" -w)
    [[ -n "$token" ]] || { printf >&2 "Missing 1Password service-account token for general\n"; exit 65; }

    export OP_SERVICE_ACCOUNT_TOKEN="$token"
    unset token VIRTUAL_ENV PYTHONHOME PYTHONPATH

    bootstrap_root="/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap"
    bootstrap_python="$bootstrap_root/venv/bin/python"
    bootstrap_script="$bootstrap_root/hermes_gateway_sdk_bootstrap.py"
    [[ -x "$bootstrap_python" && -x "$bootstrap_script" ]] || {
      printf >&2 "Missing Hermes SDK bootstrap runtime\n"
      exit 66
    }

    exec "$bootstrap_python" "$bootstrap_script" "$profile" --command desktop
  ' hermes-desktop-clean
