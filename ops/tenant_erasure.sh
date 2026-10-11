#!/bin/bash
# Tenant erasure cascade (GDPR). Dry run by default (rolls back); add --execute to commit.
set -e
cd ~/vibeforge
TID="$1"; MODE="$2"
[ -n "$TID" ] || { echo "usage: ops/tenant_erasure.sh <tenant-uuid> [--execute]"; exit 1; }
P() { docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' _ "$@"; }
TABLES=$(P -At -c "select table_name from information_schema.columns where table_schema='public' and column_name='tenant_id' order by 1")
{
  echo "BEGIN; SET session_replication_role = replica;"
  echo "INSERT INTO audit_log (tenant_id, action, resource_type, resource_id, diff_data) VALUES ('$TID','tenant.erased','tenant','$TID','{}');"
  for t in $TABLES; do [ "$t" = audit_log ] || echo "DELETE FROM \"$t\" WHERE tenant_id='$TID';"; done
  echo "DELETE FROM tenants WHERE id='$TID';"
  [ "$MODE" = "--execute" ] && echo "COMMIT;" || echo "ROLLBACK;"
} | P
[ "$MODE" = "--execute" ] && echo "ERASED $TID (audit_log keeps one tombstone row; remove Gitea repos and MinIO objects for this tenant separately)" || echo "DRY RUN only - nothing deleted"
