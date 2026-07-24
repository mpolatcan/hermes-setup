#!/bin/bash
# Resolve a profile's Quicksilver secrets and execute only the restricted Hermes send path.
set -euo pipefail

exec /usr/bin/env -i \
  HOME="/Users/mutlupolatcan" USER="mutlupolatcan" LOGNAME="mutlupolatcan" \
  LANG="${LANG:-en_US.UTF-8}" \
  PATH="/Users/mutlupolatcan/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  /bin/bash --noprofile --norc -c '
    set -euo pipefail

    profile="${1:-}"
    case "$profile" in
      general|assistant|researcher|coder|writer|producer|marketing|health|finance) ;;
      *) printf >&2 "Usage: hermes-send-keychain <profile> [Hermes send arguments]\n"; exit 64 ;;
    esac
    shift

    service="com.polatcangames.hermes.op-service-account"
    token=$(/usr/bin/security find-generic-password -s "$service" -a "$profile" -w)
    [[ -n "$token" ]] || { printf >&2 "Missing 1Password service-account token for %s\n" "$profile"; exit 65; }

    export OP_SERVICE_ACCOUNT_TOKEN="$token"
    unset token VIRTUAL_ENV PYTHONHOME PYTHONPATH

    bootstrap_root="/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap"
    bootstrap_python="$bootstrap_root/venv/bin/python"
    bootstrap_script="$bootstrap_root/hermes_gateway_sdk_bootstrap.py"
    [[ -x "$bootstrap_python" && -x "$bootstrap_script" ]] || {
      printf >&2 "Missing Hermes SDK bootstrap runtime\n"
      exit 66
    }

    exec "$bootstrap_python" "$bootstrap_script" "$profile" --command send -- "$@"
  ' hermes-send-clean "$@"
