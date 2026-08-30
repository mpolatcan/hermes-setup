#!/bin/bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
SOURCE_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
RUNTIME_ROOT="/Users/mutlupolatcan/.hermes/runtime/honcho-codex-adapter"
LAUNCHER_TARGET="/Users/mutlupolatcan/.hermes/scripts/honcho-codex-adapter-keychain.sh"
HERMES_ROOT="/Users/mutlupolatcan/.hermes/runtime/hermes-agent"
HERMES_PYTHON="$HERMES_ROOT/venv/bin/python"
SDK_PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13"

for executable in "$HERMES_PYTHON" "$SDK_PYTHON" /usr/bin/rsync; do
  if [[ ! -x "$executable" ]]; then
    printf 'required executable is unavailable: %s\n' "$executable" >&2
    exit 1
  fi
done

mkdir -p "$RUNTIME_ROOT"
stage="$RUNTIME_ROOT/.source-stage"
previous="$RUNTIME_ROOT/.source-previous"
rm -rf "$stage" "$previous"
mkdir -p "$stage"

/usr/bin/rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.venv-onepassword-sdk/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  --exclude '__pycache__/' \
  "$SOURCE_ROOT/" "$stage/"

if [[ -d "$RUNTIME_ROOT/source" ]]; then
  mv "$RUNTIME_ROOT/source" "$previous"
fi
mv "$stage" "$RUNTIME_ROOT/source"
rm -rf "$previous"

unset PYTHONPATH
"$HERMES_PYTHON" -m venv --clear "$RUNTIME_ROOT/adapter-venv"
env -u PYTHONPATH "$RUNTIME_ROOT/adapter-venv/bin/python" -m pip install \
  --only-binary=:all: \
  --constraint "$RUNTIME_ROOT/source/requirements/runtime-constraints.txt" \
  "$RUNTIME_ROOT/source"

"$SDK_PYTHON" -m venv --clear "$RUNTIME_ROOT/onepassword-sdk-venv"
env -u PYTHONPATH "$RUNTIME_ROOT/onepassword-sdk-venv/bin/python" -m pip install \
  --only-binary=:all: \
  --requirement "$RUNTIME_ROOT/source/requirements/onepassword-sdk.txt"

hermes_site=$("$HERMES_PYTHON" -c 'import site; print(site.getsitepackages()[0])')
PYTHONPATH="$HERMES_ROOT:$hermes_site" "$RUNTIME_ROOT/adapter-venv/bin/python" \
  -m honcho_codex_adapter.cli \
  --config "$RUNTIME_ROOT/source/config/adapter.toml" \
  --check-config
PYTHONPATH="$HERMES_ROOT:$hermes_site" "$RUNTIME_ROOT/adapter-venv/bin/python" \
  "$RUNTIME_ROOT/source/scripts/check_hermes_compat.py" --json >/dev/null
env -u PYTHONPATH "$RUNTIME_ROOT/onepassword-sdk-venv/bin/python" -m pip check
bash -n "$RUNTIME_ROOT/source/scripts/honcho_codex_bridge_launcher.sh"
/usr/bin/install -m 700 \
  "$RUNTIME_ROOT/source/scripts/honcho_codex_bridge_launcher.sh" \
  "$LAUNCHER_TARGET"

printf 'runtime deploy: ok\n'
