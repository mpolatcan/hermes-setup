#!/bin/zsh
set -euo pipefail
umask 077

profile="${1:?profile required}"
case "$profile" in
  general|assistant|researcher|coder|writer|producer|marketing|health|finance) ;;
  *) print -u2 -- "Unsupported Hermes profile: $profile"; exit 64 ;;
esac

service="com.polatcangames.hermes.op-service-account"
token=$(/usr/bin/security find-generic-password -s "$service" -a "$profile" -w)
[[ -n "$token" ]] || { print -u2 -- "Missing 1Password service-account token for $profile"; exit 65; }

export OP_SERVICE_ACCOUNT_TOKEN="$token"
unset token VIRTUAL_ENV
export PATH="/Users/mutlupolatcan/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export HERMES_HOME="/Users/mutlupolatcan/.hermes/profiles/$profile"

exec /Users/mutlupolatcan/.hermes/runtime/hermes-agent/venv/bin/hermes \
  --profile "$profile" gateway run --replace
