#!/bin/bash
# Recreate the pinned Honcho stack with secrets resolved fail-closed via the official 1Password SDK.
set -euo pipefail
umask 077

ROOT="/Users/mutlupolatcan/.hermes"
STACK="$ROOT/services/honcho-stack/server"
RUNTIME="$ROOT/runtime/honcho-codex-adapter"
SERVICE="com.polatcangames.hermes.op-service-account"
ADAPTER_REFERENCE="op://xaegpgrxyvqpb7dkrmlxpj2xbe/oe3kgxx7h6fb4axvnnejyw4aoa/jthjqdc63c6svaaiisuz3f3iwm"
EMBEDDING_REFERENCE="op://xaegpgrxyvqpb7dkrmlxpj2xbe/oe3kgxx7h6fb4axvnnejyw4aoa/OPENROUTER_API_KEY"
SDK_PYTHON="$RUNTIME/onepassword-sdk-venv/bin/python"
RESOLVER="$RUNTIME/source/scripts/resolve_onepassword_secret.py"
export DOCKER_HOST="unix:///Users/mutlupolatcan/.orbstack/run/docker.sock"

for required in "$SDK_PYTHON" "$RESOLVER"; do
  [ -e "$required" ] || { printf 'required secret resolver artifact unavailable\n' >&2; exit 66; }
done

token=$(/usr/bin/security find-generic-password -s "$SERVICE" -a general -w)
[ -n "$token" ] || { printf 'missing 1Password bootstrap token\n' >&2; exit 65; }
export OP_SERVICE_ACCOUNT_TOKEN="$token"
unset token

adapter_root=$(env -u PYTHONPATH "$SDK_PYTHON" "$RESOLVER" "$ADAPTER_REFERENCE")
embedding_key=$(env -u PYTHONPATH "$SDK_PYTHON" "$RESOLVER" "$EMBEDDING_REFERENCE")
[ -n "$adapter_root" ] && [ -n "$embedding_key" ] || {
  printf '1Password SDK returned an empty secret\n' >&2
  exit 65
}

export HONCHO_JWT_ROOT="$adapter_root"
auth_secret=$(env -u PYTHONPATH "$SDK_PYTHON" -c \
  'import hashlib,hmac,os; print(hmac.new(os.environ["HONCHO_JWT_ROOT"].encode(), b"honcho-auth-jwt-secret-v1", hashlib.sha256).hexdigest())')
[ -n "$auth_secret" ] || { printf 'Honcho auth derivation failed\n' >&2; exit 65; }

export HONCHO_CODEX_ADAPTER_API_KEY="$adapter_root"
export AUTH_JWT_SECRET="$auth_secret"
export LLM_OPENAI_API_KEY="$embedding_key"
unset HONCHO_JWT_ROOT adapter_root auth_secret embedding_key OP_SERVICE_ACCOUNT_TOKEN ADAPTER_REFERENCE EMBEDDING_REFERENCE
trap 'unset HONCHO_CODEX_ADAPTER_API_KEY AUTH_JWT_SECRET LLM_OPENAI_API_KEY' EXIT

if [ "${HONCHO_RECREATE_CHECK_ONLY:-0}" = "1" ]; then
  [ -n "$HONCHO_CODEX_ADAPTER_API_KEY" ] && [ -n "$AUTH_JWT_SECRET" ] && [ -n "$LLM_OPENAI_API_KEY" ] || exit 65
  printf '{"ok":true,"resolved_env_names":["AUTH_JWT_SECRET","HONCHO_CODEX_ADAPTER_API_KEY","LLM_OPENAI_API_KEY"]}\n'
  exit 0
fi

cd "$STACK"
docker compose config -q
if [ "$#" -eq 0 ]; then
  set -- database redis api deriver
fi
docker compose build --no-cache api
docker compose up -d --force-recreate "$@"
