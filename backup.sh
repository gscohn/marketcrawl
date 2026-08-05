#!/usr/bin/env bash
#
# Take a consistent snapshot of the subscriber database.
#
#     /home/sportscrawl/app/backup.sh
#
# Run daily by sportscrawl-backup.timer. The NAS then pulls the output
# directory; see docs/BACKUP.md for the whole chain and why it is a pull.
#
# This does NOT copy the database file. The live database runs in WAL mode, so
# at any moment the committed data is split between sportscrawl.db and
# sportscrawl.db-wal. Right now that is a 4KB main file and a 3MB WAL: a `cp`
# of the .db alone would produce a file that opens cleanly, reports no error,
# and is missing nearly everything. sqlite3's backup API walks the live
# database and writes one consistent file, safely, while the app keeps writing.

set -euo pipefail

APP_DIR=/home/sportscrawl/app
ENV_FILE=/home/sportscrawl/.env
BACKUP_DIR=/home/sportscrawl/backups
KEEP_DAYS=14

PY="$APP_DIR/.venv/bin/python"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TARGET="$BACKUP_DIR/sportscrawl-$STAMP.db"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
cd "$APP_DIR"

SPORTSCRAWL_ENV_FILE="$ENV_FILE" "$PY" - "$TARGET" <<'PYTHON'
import sqlite3
import sys

from sportscrawl.config import settings

target = sys.argv[1]
source = sqlite3.connect(settings()["DATABASE_PATH"])
destination = sqlite3.connect(target)

# The backup API, not a file copy. It holds a read transaction per step and
# produces a file that is consistent even though the scheduler and poller are
# writing throughout.
with destination:
    source.backup(destination)

destination.close()
source.close()

# Prove the copy is readable and populated before anything is pruned. A backup
# nobody has opened is a hypothesis.
check = sqlite3.connect(target)
integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
subscribers = check.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
events = check.execute("SELECT COUNT(*) FROM events").fetchone()[0]
teams = check.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
check.close()

if integrity != "ok":
    raise SystemExit("integrity check failed: %s" % integrity)

print("  integrity   %s" % integrity)
print("  subscribers %d" % subscribers)
print("  events      %d" % events)
print("  teams       %d" % teams)
PYTHON

gzip -f "$TARGET"
echo "  wrote       $TARGET.gz ($(stat -c %s "$TARGET.gz") bytes)"

# Prune old snapshots. The NAS keeps its own copies for longer; this is only
# the staging area the pull reads from.
find "$BACKUP_DIR" -name 'sportscrawl-*.db.gz' -mtime "+$KEEP_DAYS" -delete
echo "  retained    $(find "$BACKUP_DIR" -name 'sportscrawl-*.db.gz' | wc -l) snapshots"
