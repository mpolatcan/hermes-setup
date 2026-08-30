#!/bin/bash
set -euo pipefail

RUNTIME=/Users/mutlupolatcan/.hermes/runtime/honcho-codex-adapter

exec "$RUNTIME/adapter-venv/bin/python" \
  "$RUNTIME/source/scripts/honcho_codex_soak_probe.py" \
  --record \
  --history /Users/mutlupolatcan/.hermes/profiles/general/metrics/honcho-codex-soak.jsonl \
  --target-days 7