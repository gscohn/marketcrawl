"""Connection handling and migrations.

WAL and a busy timeout are not optional: two gunicorn workers and the
scheduler process contend for one file. Spec sections 3 and 4.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

BUSY_TIMEOUT_MS = 5000


def connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=%d" % BUSY_TIMEOUT_MS)
    # Off by default in SQLite, and the cascade deletes in section 9 depend
    # on it. It is a per-connection pragma, so it has to be set every time.
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def migrate(connection: sqlite3.Connection, migrations_dir=None) -> list:
    """Apply pending numbered migrations. Safe to run twice.

    Deliberately not an ORM auto-create: that will not alter existing tables,
    and the database outlives every deploy. Spec section 13.
    """
    directory = Path(migrations_dir or MIGRATIONS_DIR)

    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL)"
    )

    applied = {
        row["version"] for row in connection.execute("SELECT version FROM schema_version")
    }

    pending = []
    for path in sorted(directory.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version not in applied:
            pending.append((version, path))

    for version, path in pending:
        # No explicit BEGIN around this. executescript() issues an implicit
        # COMMIT before it runs, so wrapping it in a transaction leaves
        # nothing to commit and the rollback path then masks the real error.
        #
        # Atomicity comes from the migrations themselves instead: every one is
        # written IF NOT EXISTS and must be safe to run twice (spec section
        # 13). A script that dies halfway leaves its version unrecorded, so
        # the next deploy re-runs it and finishes the job.
        try:
            connection.executescript(path.read_text(encoding="utf-8"))
        except sqlite3.OperationalError as exc:
            # SQLite has no ADD COLUMN IF NOT EXISTS. A migration that only
            # adds a column is still safe to run twice, so a duplicate column
            # is treated as already applied rather than a failure.
            if "duplicate column name" not in str(exc):
                raise
        connection.execute(
            "INSERT INTO schema_version (version, applied_at)"
            " VALUES (?, datetime('now'))",
            (version,),
        )

    return [version for version, _ in pending]


def open_database(path: str) -> sqlite3.Connection:
    connection = connect(path)
    migrate(connection)
    return connection
