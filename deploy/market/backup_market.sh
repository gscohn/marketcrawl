#!/usr/bin/env bash
#
# Take a safe copy of the subscriber database. Run daily by
# marketcrawl-backup.timer; keeps the last 14 days.
#
# ELI5: the database is busy being written to, so copying the file could
# catch it half-written. SQLite's own backup function makes a clean copy
# even while the bot keeps working. (The original author's backup.sh explains
# this in more detail; this is the same idea with marketcrawl's paths and no
# home-server pull.)

set -euo pipefail

DATABASE="${DATABASE_PATH:-/home/marketcrawl/marketcrawl.db}"
BACKUP_DIR=/home/marketcrawl/backups
KEEP_DAYS=14
TARGET="$BACKUP_DIR/marketcrawl-$(date -u +%Y%m%dT%H%M%SZ).db"

# Only the marketcrawl user can read these: they list every subscriber.
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

python3 - "$DATABASE" "$TARGET" <<'PYTHON'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1])
destination = sqlite3.connect(sys.argv[2])
with destination:
    source.backup(destination)
destination.close()
source.close()

# Open the copy and check it before trusting it.
check = sqlite3.connect(sys.argv[2])
integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
subscribers = check.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
check.close()
if integrity != "ok":
    raise SystemExit("integrity check failed: %s" % integrity)
print("  integrity   %s" % integrity)
print("  subscribers %d" % subscribers)
PYTHON

gzip -f "$TARGET"
chmod 600 "$TARGET.gz"
echo "  wrote       $TARGET.gz"

find "$BACKUP_DIR" -name 'marketcrawl-*.db.gz' -mtime "+$KEEP_DAYS" -delete
echo "  kept        $(find "$BACKUP_DIR" -name 'marketcrawl-*.db.gz' | wc -l) copies"
