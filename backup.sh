#!/usr/bin/env bash
# Nightly DB backup. leads.db is the accumulated training data — losing
# it means starting the learned scorer and bandit from zero.
# Uses sqlite's online .backup (safe while jobs are writing), keeps 30 days.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="${LEADPIPE_DB:-$DIR/leads.db}"
OUT="${LEADPIPE_BACKUP_DIR:-$DIR/backup}"
KEEP_DAYS=30

if [[ ! -f "$DB" ]]; then
    echo "no db at $DB — nothing to back up"
    exit 0
fi

mkdir -p "$OUT"
stamp="$(date +%F-%H%M)"
sqlite3 "$DB" ".backup '$OUT/leads-$stamp.db'"
find "$OUT" -name 'leads-*.db' -type f -mtime +"$KEEP_DAYS" -delete
echo "backed up to $OUT/leads-$stamp.db"
