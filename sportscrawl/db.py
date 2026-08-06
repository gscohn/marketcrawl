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


def _statements(sql: str) -> list:
    """Split a migration into executable statements, dropping comments."""
    lines = [
        line for line in sql.splitlines()
        if not line.strip().startswith("--")
    ]
    joined = "\n".join(lines)
    return [part.strip() for part in joined.split(";") if part.strip()]


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
        # Statement at a time rather than executescript, so a tolerated error
        # skips one statement instead of the remainder of the file. The old
        # form recorded a migration as applied after silently abandoning
        # everything below a duplicate column.
        #
        # No explicit transaction. Atomicity comes from the migrations
        # themselves: every one is written IF NOT EXISTS and must be safe to
        # run twice (spec section 13), so a script that dies halfway leaves
        # its version unrecorded and the next deploy finishes the job.
        #
        # This splits on semicolons, so a migration containing a trigger or any
        # other body with embedded semicolons needs a different runner.
        for statement in _statements(path.read_text(encoding="utf-8")):
            try:
                connection.execute(statement)
            except sqlite3.OperationalError as exc:
                # SQLite has no ADD COLUMN IF NOT EXISTS, and every migration
                # must be safe to run twice, so this one case is expected.
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
