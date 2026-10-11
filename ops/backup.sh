#!/bin/bash
# Daily backup. RPO 24h (cron 02:00). Keeps the last 7. Restore: ops/restore.sh <backup-dir>
set -e
cd ~/vibeforge
D="${BACKUP_DIR:-$HOME/backups}/$(date +%Y%m%d-%H%M)"
mkdir -p "$D"
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB"' > "$D/postgres.dump"
for v in $(docker volume ls -q | grep -E 'gitea|minio|artifacts'); do
  docker run --rm -v "$v":/d -v "$D":/b alpine tar czf "/b/$v.tgz" -C /d .
done
ls -1dt "${BACKUP_DIR:-$HOME/backups}"/*/ | tail -n +8 | xargs -r rm -rf
echo "backup ok: $D ($(du -sh "$D" | cut -f1))"
