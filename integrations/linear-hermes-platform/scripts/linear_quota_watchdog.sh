#!/bin/sh
# Hermes cron contract: no_agent=true. This wrapper performs no Linear mutation.
set -eu

: "${LINEAR_OAUTH_FILE:?LINEAR_OAUTH_FILE is required}"
: "${LINEAR_QUOTA_STATE_DIR:?LINEAR_QUOTA_STATE_DIR is required}"
: "${LINEAR_OPERATIONS_TEAM_ID:?LINEAR_OPERATIONS_TEAM_ID is required}"

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON_BIN=${HERMES_RUNTIME_PYTHON:-/Users/mutlupolatcan/.hermes/runtime/hermes-agent/venv/bin/python}

exec "$PYTHON_BIN" "$SCRIPT_DIR/linear_quota_watchdog.py" \
  --oauth-file "$LINEAR_OAUTH_FILE" \
  --state-dir "$LINEAR_QUOTA_STATE_DIR" \
  --team-id "$LINEAR_OPERATIONS_TEAM_ID" \
  --expected-team-key "${LINEAR_OPERATIONS_TEAM_KEY:-OPS}" \
  "$@"
