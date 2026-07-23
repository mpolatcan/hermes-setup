#!/bin/bash
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${1:-/Users/mutlupolatcan/.hermes/runtime/hermes-gateway-sdk-bootstrap-candidate}"
PYTHON="${PYTHON:-/opt/homebrew/opt/python@3.13/bin/python3.13}"
UV="${UV:-/Users/mutlupolatcan/.local/bin/uv}"

[[ "$TARGET" = /* ]] || { echo "target must be absolute" >&2; exit 64; }
[[ -x "$PYTHON" ]] || { echo "missing Python 3.13 interpreter" >&2; exit 65; }
[[ -x "$UV" ]] || { echo "missing uv" >&2; exit 65; }
"$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 13) else 1)' || {
  echo "gateway bootstrap requires Python 3.13" >&2
  exit 65
}

/bin/mkdir -p "$TARGET"
"$UV" venv --clear --python "$PYTHON" "$TARGET/venv"
"$UV" pip install \
  --python "$TARGET/venv/bin/python" \
  --requirement "$ROOT/requirements/hermes-gateway-sdk.txt"
/usr/bin/install -m 700 \
  "$ROOT/scripts/hermes_gateway_sdk_bootstrap.py" \
  "$TARGET/hermes_gateway_sdk_bootstrap.py"
"$UV" pip check --python "$TARGET/venv/bin/python"
"$TARGET/venv/bin/python" -c \
  'import onepassword, yaml; print("gateway SDK bootstrap candidate: ok")'
/usr/bin/stat -f '%Sp %N' "$TARGET/hermes_gateway_sdk_bootstrap.py"
