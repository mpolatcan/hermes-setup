#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 /absolute/path/to/candidate-python [adapter-config]" >&2
  exit 64
}

[[ $# -ge 1 && $# -le 2 ]] || usage
candidate_python="$1"
config_path="${2:-config/adapter.toml}"
[[ "$candidate_python" = /* && -x "$candidate_python" ]] || {
  echo "candidate Python must be an absolute executable path" >&2
  exit 65
}
[[ -f "$config_path" ]] || { echo "missing adapter config: $config_path" >&2; exit 66; }

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
candidate_site="$($candidate_python -c 'import site; print(site.getsitepackages()[0])')"
export PYTHONPATH="$root/src:$candidate_site"

.venv/bin/python -m honcho_codex_adapter.cli --config "$config_path" --check-config
.venv/bin/python scripts/check_hermes_compat.py --json
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q src scripts tests

echo "candidate gate passed; production launcher was not changed"
echo "next gate: start an approved temporary listener on 127.0.0.1:18081 and run authenticated canaries"
