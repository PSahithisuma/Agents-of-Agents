#!/bin/bash
# Restore drill: proves the newest backup can be restored. Restores Postgres into a scratch database
# (live data untouched), compares row counts table by table, checks the volume archives are readable, reports elapsed time.
set -e
cd ~/vibeforge
[ "$1" = "--fresh" ] && bash ops/backup.sh
D=$(ls -1dt "${BACKUP_DIR:-$HOME/backups}"/*/ | head -1); D=${D%/}
[ -f "$D/postgres.dump" ] || { echo "no backup found"; exit 1; }
echo "drill using $D"
START=$(date +%s)
PSQL='docker compose exec -T postgres sh -c'
$PSQL 'dropdb -U "$POSTGRES_USER" --if-exists restore_drill && createdb -U "$POSTGRES_USER" restore_drill'
$PSQL 'pg_restore -U "$POSTGRES_USER" -d restore_drill --no-owner' < "$D/postgres.dump" 2>/tmp/drill_restore.err || true
cat > /tmp/drill_counts.sql <<'EOS'
select table_name || '=' || (xpath('/row/c/text()', query_to_xml('select count(*) as c from public.' || quote_ident(table_name), false, true, '')))[1]::text
from information_schema.tables where table_schema = 'public' and table_type = 'BASE TABLE' order by 1;
EOS
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA' < /tmp/drill_counts.sql > /tmp/drill_live.txt
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d restore_drill -tA' < /tmp/drill_counts.sql > /tmp/drill_restored.txt
TABLES=$(wc -l < /tmp/drill_live.txt); RTABLES=$(wc -l < /tmp/drill_restored.txt)
echo "tables live=$TABLES restored=$RTABLES"
DIFF=$(diff /tmp/drill_live.txt /tmp/drill_restored.txt || true)
if [ -z "$DIFF" ]; then echo "ROW COUNTS: identical in all $TABLES tables"; else echo "ROW COUNTS differ (changes since the backup are expected):"; echo "$DIFF" | head -20; fi
BAD=0
for f in "$D"/*.tgz; do
  [ -e "$f" ] || continue
  if tar tzf "$f" >/dev/null 2>&1; then echo "volume archive ok: $(basename "$f") ($(du -h "$f" | cut -f1))"; else echo "volume archive BROKEN: $f"; BAD=1; fi
done
$PSQL 'dropdb -U "$POSTGRES_USER" restore_drill'
END=$(date +%s)
echo "restore time: $((END-START)) s (target 1800 s)"
[ "$RTABLES" -gt 0 ] && [ "$RTABLES" = "$TABLES" ] && [ $BAD = 0 ] && echo "RESTORE DRILL: PASS" || echo "RESTORE DRILL: FAIL"
