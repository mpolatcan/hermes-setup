#!/bin/bash
# GIT_SSH_COMMAND boundary for Hermes profile shells. Agent Git network access
# must use an approved profile-scoped HTTPS broker; no SSH fallback is allowed.
set -euo pipefail

printf >&2 'hermes-agent-ssh-guard: GitHub SSH is disabled; all agent Git SSH transport must use an approved HTTPS route\n'
exit 69
