"""Migration runner tests."""

from __future__ import annotations

import sqlite3

import pytest

from sportscrawl.db import _statements, connect, migrate


def test_statements_drop_comments_and_split_on_semicolons():
    sql = """-- a comment
    CREATE TABLE a (x TEXT);
    -- another
    ALTER TABLE a ADD COLUMN y TEXT;
    """
    assert len(_statements(sql)) == 2


def test_a_tolerated_duplicate_column_does_not_skip_later_statements(tmp_path):
    """The old runner used executescript and abandoned everything after the
    duplicate, while still recording the migration as applied."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_base.sql").write_text(
        "CREATE TABLE IF NOT EXISTS thing (a TEXT);", encoding="utf-8"
    )
    (migrations / "002_add.sql").write_text(
        "ALTER TABLE thing ADD COLUMN a TEXT;\n"      # duplicate, tolerated
        "ALTER TABLE thing ADD COLUMN b TEXT;",       # must still run
        encoding="utf-8",
    )

    db = connect(str(tmp_path / "m.db"))
    migrate(db, migrations_dir=str(migrations))

    columns = {r[1] for r in db.execute("PRAGMA table_info(thing)")}
    assert "b" in columns


def test_a_real_error_still_raises(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_bad.sql").write_text("SELECT * FROM nope;", encoding="utf-8")

    db = connect(str(tmp_path / "m.db"))
    with pytest.raises(sqlite3.OperationalError):
        migrate(db, migrations_dir=str(migrations))


def test_migrations_are_idempotent(tmp_path):
    db = connect(str(tmp_path / "real.db"))
    first = migrate(db)
    assert first
    assert migrate(db) == []
