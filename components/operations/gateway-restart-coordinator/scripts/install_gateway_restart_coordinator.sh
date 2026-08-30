#!/bin/bash
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="/Users/mutlupolatcan/.hermes/services/gateway-restart-coordinator"
STATE_DIR="/Users/mutlupolatcan/.hermes/restart-coordinator"
PLIST_TARGET="/Users/mutlupolatcan/Library/LaunchAgents/ai.hermes.gateway-restart-coordinator.plist"

/usr/bin/install -d -m 700 "$TARGET_DIR" "$STATE_DIR"
/usr/bin/install -m 700 "$SOURCE_DIR/gateway_restartctl.py" "$TARGET_DIR/restartctl.py"
/usr/bin/install -m 600 "$SOURCE_DIR/restart_coordinator.py" "$TARGET_DIR/restart_coordinator.py"
/usr/bin/install -m 600 "$SOURCE_DIR/launchd/ai.hermes.gateway-restart-coordinator.plist" "$PLIST_TARGET"
/usr/bin/plutil -lint "$PLIST_TARGET"
/Users/mutlupolatcan/.hermes/runtime/hermes-agent/venv/bin/python -m py_compile "$TARGET_DIR/restart_coordinator.py" "$TARGET_DIR/restartctl.py"

printf 'staged=%s\nplist=%s\n' "$TARGET_DIR" "$PLIST_TARGET"
