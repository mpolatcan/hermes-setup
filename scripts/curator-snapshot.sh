#!/bin/bash
# Curator snapshot — exports skills state + archives shared-skills content
set -euo pipefail
source /Users/mutlupolatcan/.hermes/profiles/general/init.sh
BACKUP_DIR="/Users/mutlupolatcan/.hermes/backups"
mkdir -p "$BACKUP_DIR"
ts="$(date +%Y-%m-%d)"

# curator-managed (hub) skills registry — currently empty, kept for future hub installs
/opt/homebrew/bin/hermes skills snapshot export "$BACKUP_DIR/skills-$ts.json"

# the real skills: local external-dir content (not covered by snapshot export)
tar czf "$BACKUP_DIR/shared-skills-$ts.tar.gz" \
  -C /Users/mutlupolatcan/.hermes/shared-skills canonical

# rotate: keep 4 weekly archives + 4 json snapshots
ls -t "$BACKUP_DIR"/shared-skills-*.tar.gz 2>/dev/null | tail -n +5 | xargs -r rm -f
ls -t "$BACKUP_DIR"/skills-*.json 2>/dev/null | tail -n +5 | xargs -r rm -f

echo "Snapshot: skills-$ts.json + shared-skills-$ts.tar.gz"
