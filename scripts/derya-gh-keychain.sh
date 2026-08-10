#!/bin/bash
# Execute gh with a short-lived Derya GitHub App installation token.
set -euo pipefail

[[ "$#" -gt 0 ]] || { printf >&2 "usage: derya-gh <gh arguments...>\n"; exit 64; }

exec /usr/bin/env -i \
  HOME="/Users/mutlupolatcan" USER="mutlupolatcan" LOGNAME="mutlupolatcan" \
  LANG="${LANG:-en_US.UTF-8}" \
  PATH="/Users/mutlupolatcan/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  /bin/bash --noprofile --norc -c '
    set -euo pipefail
    umask 077

    token=$(/usr/bin/security find-generic-password \
      -s "com.polatcangames.hermes.op-service-account" \
      -a "general" -w)
    [[ -n "$token" ]] || { printf >&2 "derya-gh: missing 1Password bootstrap token\n"; exit 65; }

    export OP_SERVICE_ACCOUNT_TOKEN="$token"
    unset token VIRTUAL_ENV PYTHONHOME PYTHONPATH

    runtime="/Users/mutlupolatcan/.hermes/runtime/github-app-token-broker"
    python="/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap/venv/bin/python"
    broker="$runtime/github_app_token_broker.py"
    [[ -x "$python" && -f "$broker" ]] || {
      printf >&2 "derya-gh: missing managed token broker runtime\n"
      exit 66
    }

    exec "$python" "$broker" -- /opt/homebrew/bin/gh "$@"
  ' derya-gh "$@"
