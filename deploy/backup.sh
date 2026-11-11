#!/usr/bin/env bash
# Backup of the whole stack: Part-DB (database + attachments) and the
# bom-agent intake queue with its photos.
#
#   ./backup.sh [target-directory]      (default: ./backups)
#
# Run it from cron, e.g. every night at 03:00:
#   0 3 * * * /path/to/deploy/backup.sh /path/to/backups
#
# A missing database is an ERROR, not a warning: a backup without the
# database is worthless, and cron should complain about it loudly.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
TARGET="${1:-./backups}"
STAMP="$(date +%Y-%m-%d_%H%M)"
OUT="$TARGET/$STAMP"
mkdir -p "$OUT"

# Part-DB: its own backup command, as the web server user. A plain copy of
# a SQLite file in WAL mode can look fine and still be unreadable.
docker compose exec -T --user www-data partdb \
  php bin/console partdb:backup --full --overwrite -n /tmp/partdb-backup.zip
docker compose cp partdb:/tmp/partdb-backup.zip "$OUT/partdb-full.zip"
docker compose exec -T partdb rm -f /tmp/partdb-backup.zip

# bom-agent: consistent copy of the queue database (sqlite backup API), then
# the whole /data volume with the photos.
docker compose exec -T bom-agent python - <<'PY'
import sqlite3
src = sqlite3.connect("/data/bom.db")
dst = sqlite3.connect("/tmp/bom-backup.db")
src.backup(dst)
dst.close()
print("bom.db:", sqlite3.connect("/tmp/bom-backup.db").execute("pragma integrity_check").fetchone()[0])
PY
docker compose cp bom-agent:/tmp/bom-backup.db "$OUT/bom.db"
docker compose exec -T bom-agent tar -C /data --exclude=bom.db --exclude='bom.db-*' -cf - . >"$OUT/bom-agent-photos.tar"
docker compose exec -T bom-agent rm -f /tmp/bom-backup.db

cp .env "$OUT/env.backup"
chmod 600 "$OUT/env.backup"

# Keep 30 days.
find "$TARGET" -mindepth 1 -maxdepth 1 -type d -mtime +30 -exec rm -rf {} +

echo "backup written to $OUT"
ls -la "$OUT"
