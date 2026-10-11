#!/bin/bash
# Restore Postgres from a backup directory. RTO target: 30 minutes. Volumes: untar <volume>.tgz into the volume the same way.
set -e
cd ~/vibeforge
D="$1"; [ -f "$D/postgres.dump" ] || { echo "usage: ops/restore.sh <backup-dir>"; exit 1; }
read -p "This REPLACES the current database. Type yes: " a; [ "$a" = yes ] || exit 1
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < "$D/postgres.dump"
echo "restore ok from $D"
