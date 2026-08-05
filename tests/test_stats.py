"""Aggregate statistics tests. Spec section 11."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sportscrawl import repo, stats
from sportscrawl.db import connect, migrate

PEPPER = "test-pepper"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = connect(str(tmp_path / "stats.db"))
    migrate(connection)
    return connection


def signup(db, did, when):
    repo.create_subscriber(db, did, when, PEPPER, tz="America/Los_Angeles")


def test_deleted_subscribers_are_still_counted(db):
    """Unrecoverable from current state: the row is gone. This is the whole
    reason the events table outlives deletion."""
    signup(db, "did:plc:a", NOW - timedelta(days=10))
    signup(db, "did:plc:b", NOW - timedelta(days=10))
    repo.delete_subscriber(db, "did:plc:b", NOW, PEPPER)

    counts = stats.state_counts(db)
    assert counts["active"] == 1
    assert counts["deleted"] == 1
    assert db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0] == 1


def test_paused_and_deleted_are_separate_signals(db):
    """Paused suggests seasonal quiet, deleted suggests churn. That
    distinction is the metric the project exists to gather."""
    signup(db, "did:plc:a", NOW)
    signup(db, "did:plc:b", NOW)
    repo.set_state(db, "did:plc:a", "paused", NOW, PEPPER)
    repo.delete_subscriber(db, "did:plc:b", NOW, PEPPER)

    counts = stats.state_counts(db)
    assert counts["paused"] == 1
    assert counts["deleted"] == 1


def test_the_events_table_holds_no_dids(db):
    signup(db, "did:plc:secret", NOW)
    subjects = [r[0] for r in db.execute("SELECT subject FROM events")]
    assert all("did:plc:" not in s for s in subjects)
    assert all(len(s) == 64 for s in subjects)


def test_buckets_below_the_floor_are_suppressed(db):
    signup(db, "did:plc:a", NOW)
    repo.add_sub(db, "did:plc:a", "team", "baseball/mlb", "19")

    rows = stats.selections_by_league(db, floor=5)
    assert rows[0]["count"] is None

    rows = stats.selections_by_league(db, floor=1)
    assert rows[0]["count"] == 1


def test_stop_rate_and_time_to_stop(db):
    signup(db, "did:plc:a", NOW - timedelta(days=4))
    signup(db, "did:plc:b", NOW - timedelta(days=4))
    repo.set_state(db, "did:plc:a", "paused", NOW, PEPPER)

    result = stats.stop_stats(db)
    assert result["signups"] == 2
    assert result["paused"] == 1
    assert result["stop_rate"] == 50.0
    assert result["median_days_to_stop"] is not None


def test_a_timeline_nets_signups_against_deletes(db):
    signup(db, "did:plc:a", NOW)
    signup(db, "did:plc:b", NOW)
    repo.delete_subscriber(db, "did:plc:b", NOW, PEPPER)

    series = stats.signups_over_time(db)
    assert series[-1]["total"] == 1


def test_summary_runs_on_an_empty_database(db):
    """The first person to open it will be looking at nothing."""
    data = stats.summary(db)
    assert data["states"] == {"active": 0, "paused": 0, "deleted": 0}
    assert data["retention"][0]["pct"] is None
