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


# --------------------------------------------------------------------------
# Cohort retention
# --------------------------------------------------------------------------


def at(days_ago, hours=0):
    return NOW - timedelta(days=days_ago, hours=hours)


def event(db, did, kind, when):
    repo.record_event(db, when, kind, did, PEPPER)


def test_someone_who_paused_and_came_back_is_retained(db):
    """The old query counted any pause forever, so a February pause and a
    March resume read as churn for the rest of time."""
    event(db, "did:plc:a", "signup", at(60))
    event(db, "did:plc:a", "pause", at(50))
    event(db, "did:plc:a", "resume", at(45))

    thirty = next(r for r in stats.retention(db, now=NOW) if r["days"] == 30)
    assert thirty["cohort"] == 1
    assert thirty["retained"] == 1


def test_a_pause_after_the_window_does_not_count_against_it(db):
    """Signed up 60 days ago, paused yesterday. Still retained at day 7."""
    event(db, "did:plc:b", "signup", at(60))
    event(db, "did:plc:b", "pause", at(1))

    result = {r["days"]: r for r in stats.retention(db, now=NOW)}
    assert result[7]["retained"] == 1
    assert result[30]["retained"] == 1


def test_a_pause_inside_the_window_does_count(db):
    event(db, "did:plc:c", "signup", at(60))
    event(db, "did:plc:c", "pause", at(57))

    result = {r["days"]: r for r in stats.retention(db, now=NOW)}
    assert result[7]["retained"] == 0
    assert result[30]["retained"] == 0


def test_someone_too_new_is_not_in_the_cohort(db):
    """They have not reached day 30 yet, so counting them would drag the rate
    down for no reason."""
    event(db, "did:plc:d", "signup", at(3))

    result = {r["days"]: r for r in stats.retention(db, now=NOW)}
    assert result[7]["cohort"] == 0
    assert result[30]["cohort"] == 0


def test_a_deletion_inside_the_window_is_churn(db):
    event(db, "did:plc:e", "signup", at(40))
    event(db, "did:plc:e", "delete", at(38))

    thirty = next(r for r in stats.retention(db, now=NOW) if r["days"] == 30)
    assert thirty["cohort"] == 1 and thirty["retained"] == 0


def test_the_windows_can_differ(db):
    """The old query made 7, 30 and 90 report the same thing."""
    event(db, "did:plc:f", "signup", at(100))
    event(db, "did:plc:f", "pause", at(80))      # day 20 of their life

    result = {r["days"]: r for r in stats.retention(db, now=NOW)}
    assert result[7]["retained"] == 1            # still there at day 7
    assert result[30]["retained"] == 0           # gone by day 30


# --------------------------------------------------------------------------
# Orphaned subscriptions
# --------------------------------------------------------------------------


def test_a_sub_to_a_departed_club_is_reported(db):
    """The only thing that surfaces it: the silence is byte-identical to the
    intentional empty-digest rule."""
    signup(db, "did:plc:a", NOW)
    db.execute(
        "INSERT INTO teams (league, team_id, short_name, active)"
        " VALUES ('soccer/eng.1', '7', 'Relegated FC', 0)"
    )
    repo.add_sub(db, "did:plc:a", "team", "soccer/eng.1", "7")

    orphans = stats.orphaned_subs(db)
    assert len(orphans) == 1
    assert orphans[0]["name"] == "Relegated FC"
    assert orphans[0]["subscribers"] == 1
    assert orphans[0]["reason"] == "left the league"


def test_a_sub_to_an_active_club_is_not_reported(db):
    signup(db, "did:plc:b", NOW)
    db.execute(
        "INSERT INTO teams (league, team_id, short_name, active)"
        " VALUES ('baseball/mlb', '19', 'Dodgers', 1)"
    )
    repo.add_sub(db, "did:plc:b", "team", "baseball/mlb", "19")
    assert stats.orphaned_subs(db) == []


def test_a_sub_with_no_team_row_at_all_is_reported(db):
    signup(db, "did:plc:c", NOW)
    repo.add_sub(db, "did:plc:c", "team", "soccer/eng.1", "ghost")
    orphans = stats.orphaned_subs(db)
    assert orphans[0]["reason"] == "not in table"


def test_competition_subs_are_never_orphans(db):
    """Tournament slugs are deliberately unseeded and subscribed whole."""
    signup(db, "did:plc:d", NOW)
    repo.add_sub(db, "did:plc:d", "competition", "soccer/fifa.world")
    assert stats.orphaned_subs(db) == []
