#!/bin/bash
# Git credential helper for the Derya GitHub App broker.
# Called by git as:  derya-gh-credential get|store|erase
# Reads the profile-scoped 1Password service-account bootstrap from Keychain,
# exports it as OP_SERVICE_ACCOUNT_TOKEN (process memory only), and dispatches
# to the broker's --credential surface. store/erase are no-ops (fail-closed).
set -euo pipefail
umask 077

[[ "$#" -eq 1 ]] || { printf >&2 "derya-gh-credential: expected exactly one operation\n"; exit 64; }
case "$1" in
  get|store|erase) ;;
  *) printf >&2 "derya-gh-credential: unsupported operation '%s'\n" "$1"; exit 64 ;;
esac

token=$(/usr/bin/security find-generic-password \
  -s "com.polatcangames.hermes.op-service-account" \
  -a "general" -w)
[[ -n "$token" ]] || { printf >&2 "derya-gh-credential: missing 1Password bootstrap token\n"; exit 65; }

export OP_SERVICE_ACCOUNT_TOKEN="$token"
unset token VIRTUAL_ENV PYTHONHOME PYTHONPATH

runtime="/Users/mutlupolatcan/.hermes/runtime/github-app-token-broker"
python="/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap/venv/bin/python"
broker="$runtime/github_app_token_broker.py"
[[ -x "$python" && -f "$broker" ]] || {
  printf >&2 "derya-gh-credential: missing managed token broker runtime\n"
  exit 66
}

exec "$python" "$broker" --credential "$1"
