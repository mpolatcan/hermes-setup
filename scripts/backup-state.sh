#!/bin/bash
# Hermes state backup — weekly via launchd
BACKUP_DIR="/Users/mutlupolatcan/.hermes/state-backups"
TIMESTAMP=$(date +%Y%m%d)
mkdir -p "$BACKUP_DIR"

tar czf "$BACKUP_DIR/hermes-state-$TIMESTAMP.tar.gz" \
  -C /Users/mutlupolatcan/.hermes/profiles \
  general/config.yaml general/memories \
  assistant/config.yaml assistant/memories \
  coder/config.yaml coder/memories \
  finance/config.yaml finance/memories \
  health/config.yaml health/memories \
  marketing/config.yaml marketing/memories \
  producer/config.yaml producer/memories \
  researcher/config.yaml researcher/memories \
  writer/config.yaml writer/memories \
  general/logs assistant/logs coder/logs finance/logs health/logs \
  marketing/logs producer/logs researcher/logs writer/logs \
  2>/dev/null

# root-level config: honcho fleet map, kanban board, active profile, root config
tar czf "$BACKUP_DIR/hermes-root-$TIMESTAMP.tar.gz" \
  -C /Users/mutlupolatcan/.hermes \
  honcho.json kanban.db active_profile config.yaml SOUL.md scripts \
  2>/dev/null
find "$BACKUP_DIR" -name "hermes-root-*.tar.gz" -mtime +28 -delete

find "$BACKUP_DIR" -name "hermes-state-*.tar.gz" -mtime +28 -delete
echo "[$TIMESTAMP] State backup: hermes-state-$TIMESTAMP.tar.gz" >> "$BACKUP_DIR/backup.log"
