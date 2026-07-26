#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 /absolute/path/to/candidate-python [adapter-config] [adapter-python]" >&2
  exit 64
}

[[ $# -ge 1 && $# -le 3 ]] || usage
root="$(cd "$(dirname "$0")/.." && pwd)"
candidate_python="$1"
config_path="${2:-$root/config/adapter.toml}"
adapter_python="${3:-$root/.venv/bin/python}"
[[ "$candidate_python" = /* && -x "$candidate_python" ]] || {
  echo "candidate Python must be an absolute executable path" >&2
  exit 65
}
[[ "$adapter_python" = /* && -x "$adapter_python" ]] || {
  echo "adapter Python must be an absolute executable path" >&2
  exit 65
}
candidate_abi="$($candidate_python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
adapter_abi="$($adapter_python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[[ "$candidate_abi" == "$adapter_abi" ]] || {
  echo "candidate and adapter Python major/minor versions must match" >&2
  exit 65
}
[[ -f "$config_path" ]] || { echo "missing adapter config: $config_path" >&2; exit 66; }

cd "$root"
candidate_site="$($candidate_python -c 'import site; print(site.getsitepackages()[0])')"
candidate_root="$($candidate_python -c 'from pathlib import Path; import hermes_cli; print(Path(hermes_cli.__file__).resolve().parents[1])')"
setup_root="$(cd "$root/../.." && pwd)"
patch_manager="$setup_root/scripts/manage_hermes_agent_patches.py"
[[ -f "$patch_manager" ]] || { echo "missing Hermes patch manager: $patch_manager" >&2; exit 66; }
"$adapter_python" "$patch_manager" --runtime-root "$candidate_root" --mode check
export PYTHONPATH="$root/src:$candidate_root:$candidate_site"

"$adapter_python" -m honcho_codex_adapter.cli --config "$config_path" --check-config
"$adapter_python" scripts/check_hermes_compat.py --json
"$adapter_python" -m unittest discover -s tests -v
"$adapter_python" -m compileall -q src scripts tests

echo "candidate gate passed; production launcher was not changed"
echo "next gate: start an approved temporary listener on 127.0.0.1:18081 and run authenticated canaries"
