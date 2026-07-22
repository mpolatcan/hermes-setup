#!/bin/bash
set -euo pipefail

exec /Users/mutlupolatcan/Desktop/hermes-setup/integrations/honcho-codex-adapter/.venv/bin/python \
  /Users/mutlupolatcan/Desktop/hermes-setup/integrations/honcho-codex-adapter/scripts/honcho_codex_soak_probe.py \
  --record \
  --history /Users/mutlupolatcan/.hermes/profiles/general/metrics/honcho-codex-soak.jsonl \
  --target-days 7