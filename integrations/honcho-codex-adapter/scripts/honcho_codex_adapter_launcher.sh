#!/bin/bash
set -euo pipefail
umask 077

RUNTIME="/Users/mutlupolatcan/.hermes/runtime/honcho-codex-adapter"
SERVICE="com.polatcangames.hermes.op-service-account"
REFERENCE="op://xaegpgrxyvqpb7dkrmlxpj2xbe/oe3kgxx7h6fb4axvnnejyw4aoa/jthjqdc63c6svaaiisuz3f3iwm"
HERMES_PYTHON="/opt/homebrew/opt/hermes-agent/libexec/bin/python"
ADAPTER_PYTHON="$RUNTIME/adapter-venv/bin/python"
SDK_PYTHON="$RUNTIME/onepassword-sdk-venv/bin/python"
RESOLVER="$RUNTIME/source/scripts/resolve_onepassword_secret.py"
CONFIG="$RUNTIME/source/config/adapter.toml"

for executable in "$HERMES_PYTHON" "$ADAPTER_PYTHON" "$SDK_PYTHON"; do
  if [[ ! -x "$executable" ]]; then
    printf 'required runtime executable is unavailable\n' >&2
    exit 66
  fi
done
[[ -r "$RESOLVER" && -r "$CONFIG" ]] || {
  printf 'required runtime artifact is unavailable\n' >&2
  exit 66
}

token=$(/usr/bin/security find-generic-password -s "$SERVICE" -a general -w)
[[ -n "$token" ]] || {
  printf 'missing 1Password service-account bootstrap token\n' >&2
  exit 65
}

export OP_SERVICE_ACCOUNT_TOKEN="$token"
unset token VIRTUAL_ENV PYTHONHOME PYTHONPATH
secret=$(env -u PYTHONPATH "$SDK_PYTHON" "$RESOLVER" "$REFERENCE")
[[ -n "$secret" ]] || {
  printf '1Password SDK returned an empty secret\n' >&2
  exit 65
}
export HONCHO_CODEX_ADAPTER_API_KEY="$secret"
unset secret OP_SERVICE_ACCOUNT_TOKEN REFERENCE

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export HERMES_HOME="/Users/mutlupolatcan/.hermes/profiles/general"
export HONCHO_CODEX_QUEUE_CAPACITY=8
export HONCHO_CODEX_UPSTREAM_TIMEOUT_SECONDS=90
export HONCHO_CODEX_UPSTREAM_MAX_RETRIES=0
export PYTHONPATH="$($HERMES_PYTHON -c 'import site; print(site.getsitepackages()[0])')"

exec "$ADAPTER_PYTHON" \
  -m honcho_codex_adapter.cli \
  --config "$CONFIG"
