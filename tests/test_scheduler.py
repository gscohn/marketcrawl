"""Scheduler tests. Spec sections 7 and 15."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sportscrawl import repo
from sportscrawl.db import connect, migrate
from sportscrawl.models import Event, Team
from sportscrawl.scheduler import (
    DUE,
    EXPIRED,
    NOT_DUE,
    Scheduler,
    classify,
)
from sportscrawl.source import WindowResult

MLB = "baseball/mlb"
MLS = "soccer/usa.1"
PACIFIC = "America/Los_Angeles"
PEPPER = "test-pepper"

DID = "did:plc:subscriber"


@pytest.fixture
def db(tmp_path):
    connection = connect(str(tmp_path / "test.db"))
    migrate(connection)
    return connection


def add_subscriber(db, did=DID, tz=PACIFIC, send_time="07:00", state="active"):
    when = datetime(2026, 8, 1, tzinfo=timezone.utc)
    repo.create_subscriber(db, did, when, PEPPER, tz=tz, send_time=send_time)
    if state != "active":
        db.execute("UPDATE subscribers SET state = ? WHERE did = ?", (state, did))
    return did


def team(team_id, name):
    return Team(team_id=team_id, name=name, abbrev=name[:3].upper())


def final(league=MLB, start=None, home_id="100", away_id="200"):
    return Event(
        event_id="e1",
        league=league,
        start=start or datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
        state="post",
        home=team(home_id, "Dodgers"),
        away=team(away_id, "Padres"),
        home_score=6,
        away_score=3,
        winner="home",
    )


class FakeSource:
    def __init__(self, events=None, failed=None):
        self.result = WindowResult(
            events=events or [], failed_leagues=set(failed or ())
        )
        self.health = {}
        self.calls = 0

    def fetch_window(self, leagues, now):
        self.calls += 1
        return self.result


class Recorder:
    def __init__(self, fail_after=None):
        self.sent = []
        self.fail_after = fail_after

    def __call__(self, did, text):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise RuntimeError("chat transport exploded")
        self.sent.append((did, text))


def scheduler(db, source, sender, leagues=(MLB,)):
    return Scheduler(
        db, source, leagues, PEPPER, sender=sender, sleep=lambda _: None, gap=0
    )


def utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# The lateness window
# --------------------------------------------------------------------------


def test_not_due_before_send_time(db):
    add_subscriber(db)
    row = repo.get_subscriber(db, DID)
    verdict, _ = classify(row, utc(2026, 8, 4, 13, 0))  # 06:00 Pacific
    assert verdict == NOT_DUE


def test_due_at_send_time(db):
    add_subscriber(db)
    row = repo.get_subscriber(db, DID)
    verdict, _ = classify(row, utc(2026, 8, 4, 14, 0))  # 07:00 Pacific
    assert verdict == DUE


def test_still_due_eighty_nine_minutes_late(db):
    """A reboot or a slow deploy must not drop the day."""
    add_subscriber(db)
    row = repo.get_subscriber(db, DID)
    verdict, _ = classify(row, utc(2026, 8, 4, 15, 29))
    assert verdict == DUE


def test_expired_past_the_window(db):
    """A breakfast digest at teatime is worse than no digest."""
    add_subscriber(db)
    row = repo.get_subscriber(db, DID)
    verdict, _ = classify(row, utc(2026, 8, 4, 15, 31))
    assert verdict == EXPIRED


def test_signing_up_long_after_send_time_does_not_trigger_an_instant_digest(db):
    add_subscriber(db)
    source = FakeSource(events=[final()])
    repo.add_sub(db, DID, "team", MLB, "100")
    sender = Recorder()

    scheduler(db, source, sender).tick(utc(2026, 8, 4, 22, 0))  # 15:00 Pacific

    assert sender.sent == []
    assert repo.get_subscriber(db, DID)["last_evaluated_date"] == "2026-08-04"


def test_already_evaluated_today_is_not_reconsidered(db):
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    source = FakeSource(events=[final()])
    sender = Recorder()
    engine = scheduler(db, source, sender)

    engine.tick(utc(2026, 8, 4, 14, 0))
    engine.tick(utc(2026, 8, 4, 14, 1))
    engine.tick(utc(2026, 8, 4, 14, 30))

    assert len(sender.sent) == 1


# --------------------------------------------------------------------------
# Who gets skipped
# --------------------------------------------------------------------------


def test_a_paused_subscriber_gets_nothing(db):
    add_subscriber(db, state="paused")
    repo.add_sub(db, DID, "team", MLB, "100")
    sender = Recorder()
    scheduler(db, FakeSource(events=[final()]), sender).tick(utc(2026, 8, 4, 14, 0))
    assert sender.sent == []


def test_a_subscriber_with_no_timezone_is_skipped(db):
    add_subscriber(db, tz=None)
    repo.add_sub(db, DID, "team", MLB, "100")
    sender = Recorder()
    scheduler(db, FakeSource(events=[final()]), sender).tick(utc(2026, 8, 4, 14, 0))
    assert sender.sent == []


def test_delete_landing_mid_cycle_stops_the_send(db):
    """Otherwise someone gets messaged after being told they were removed."""
    add_subscriber(db, did="did:plc:first")
    add_subscriber(db, did="did:plc:second")
    repo.add_sub(db, "did:plc:first", "team", MLB, "100")
    repo.add_sub(db, "did:plc:second", "team", MLB, "100")

    class DeletingRecorder(Recorder):
        def __call__(self, did, text):
            db.execute("DELETE FROM subscribers WHERE did = 'did:plc:second'")
            super().__call__(did, text)

    sender = DeletingRecorder()
    scheduler(db, FakeSource(events=[final()]), sender).tick(utc(2026, 8, 4, 14, 0))

    assert len(sender.sent) == 1
    assert sender.sent[0][0] == "did:plc:first"


# --------------------------------------------------------------------------
# Fan out
# --------------------------------------------------------------------------


def test_one_fetch_serves_every_subscriber(db):
    """A hundred subscribers should still cost single digit API calls."""
    for index in range(25):
        did = "did:plc:sub%d" % index
        add_subscriber(db, did=did)
        repo.add_sub(db, did, "team", MLB, "100")

    source = FakeSource(events=[final()])
    sender = Recorder()
    scheduler(db, source, sender).tick(utc(2026, 8, 4, 14, 0))

    assert source.calls == 1
    assert len(sender.sent) == 25


def test_nothing_is_fetched_when_nobody_is_due(db):
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    source = FakeSource(events=[final()])

    scheduler(db, source, Recorder()).tick(utc(2026, 8, 4, 12, 0))
    assert source.calls == 0


# --------------------------------------------------------------------------
# The commit table, spec section 7
# --------------------------------------------------------------------------


def test_a_digest_advances_last_sent_only(db):
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    scheduler(db, FakeSource(events=[final()]), Recorder()).tick(utc(2026, 8, 4, 14, 0))

    row = repo.get_subscriber(db, DID)
    assert row["last_sent_date"] == "2026-08-04"
    assert row["last_evaluated_date"] == "2026-08-04"
    assert row["last_failure_date"] is None


def test_silence_advances_evaluated_only(db):
    """No games is not an error and gets no message, but the subscriber must
    stop being reconsidered every minute for the rest of the day."""
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    sender = Recorder()
    scheduler(db, FakeSource(events=[]), sender).tick(utc(2026, 8, 4, 14, 0))

    row = repo.get_subscriber(db, DID)
    assert sender.sent == []
    assert row["last_evaluated_date"] == "2026-08-04"
    assert row["last_sent_date"] is None


def test_a_partial_advances_both_dates(db):
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    repo.add_sub(db, DID, "competition", MLS)
    source = FakeSource(events=[final()], failed=[MLS])
    sender = Recorder()

    scheduler(db, source, sender, leagues=(MLB, MLS)).tick(utc(2026, 8, 4, 14, 20))

    row = repo.get_subscriber(db, DID)
    assert row["last_sent_date"] == "2026-08-04"
    assert row["last_failure_date"] == "2026-08-04"
    assert "Dodgers" in sender.sent[0][1]


def test_a_total_failure_advances_failure_only(db):
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    source = FakeSource(events=[], failed=[MLB])
    sender = Recorder()

    scheduler(db, source, sender).tick(utc(2026, 8, 4, 14, 20))

    row = repo.get_subscriber(db, DID)
    assert row["last_failure_date"] == "2026-08-04"
    assert row["last_sent_date"] is None
    assert len(sender.sent) == 1


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_a_failing_league_is_retried_before_apologising(db):
    """A three minute blip landing on 07:00 must not cost the digest."""
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    source = FakeSource(events=[], failed=[MLB])
    sender = Recorder()
    engine = scheduler(db, source, sender)

    report = engine.tick(utc(2026, 8, 4, 14, 3))
    assert report.deferred == 1
    assert sender.sent == []
    assert repo.get_subscriber(db, DID)["last_evaluated_date"] is None

    # The blip clears within the grace period.
    source.result = WindowResult(events=[final()], failed_leagues=set())
    engine.tick(utc(2026, 8, 4, 14, 6))
    assert len(sender.sent) == 1
    assert "Dodgers" in sender.sent[0][1]


def test_the_apology_arrives_once_the_grace_period_expires(db):
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    source = FakeSource(events=[], failed=[MLB])
    sender = Recorder()
    engine = scheduler(db, source, sender)

    engine.tick(utc(2026, 8, 4, 14, 5))
    assert sender.sent == []

    engine.tick(utc(2026, 8, 4, 14, 11))
    assert len(sender.sent) == 1
    assert "isn't responding" in sender.sent[0][1]


def test_one_outage_produces_at_most_one_notice_per_subscriber(db):
    """A single outage must not become 200 apologies."""
    for index in range(5):
        did = "did:plc:sub%d" % index
        add_subscriber(db, did=did)
        repo.add_sub(db, did, "team", MLB, "100")

    source = FakeSource(events=[], failed=[MLB])
    sender = Recorder()
    engine = scheduler(db, source, sender)

    engine.tick(utc(2026, 8, 4, 14, 11))
    engine.tick(utc(2026, 8, 4, 14, 20))
    engine.tick(utc(2026, 8, 4, 15, 0))

    assert len(sender.sent) == 5
    assert len({did for did, _ in sender.sent}) == 5


def test_a_failure_notice_is_not_repeated_after_a_manual_reset(db):
    """Guarded by last_failure_date, not just by last_evaluated_date."""
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    source = FakeSource(events=[], failed=[MLB])
    sender = Recorder()
    engine = scheduler(db, source, sender)

    engine.tick(utc(2026, 8, 4, 14, 11))
    assert len(sender.sent) == 1

    db.execute("UPDATE subscribers SET last_evaluated_date = NULL WHERE did = ?", (DID,))
    engine.tick(utc(2026, 8, 4, 14, 20))
    assert len(sender.sent) == 1


# --------------------------------------------------------------------------
# Crash mid batch
# --------------------------------------------------------------------------


def test_a_crash_mid_batch_resumes_without_resending(db):
    """Per message commit is what makes a deploy at 07:05 safe."""
    for index in range(5):
        did = "did:plc:sub%d" % index
        add_subscriber(db, did=did)
        repo.add_sub(db, did, "team", MLB, "100")

    source = FakeSource(events=[final()])
    sender = Recorder(fail_after=2)
    engine = scheduler(db, source, sender)

    report = engine.tick(utc(2026, 8, 4, 14, 0))

    # The failures are isolated: the batch finishes rather than aborting, and
    # the subscribers who failed are left unevaluated so the next tick retries.
    assert len(sender.sent) == 2
    assert report.errors == 3
    delivered = {did for did, _ in sender.sent}

    # Restart: a fresh sender, the same database.
    resumed = Recorder()
    Scheduler(db, source, [MLB], PEPPER, sender=resumed, sleep=lambda _: None, gap=0).tick(
        utc(2026, 8, 4, 14, 2)
    )

    assert len(resumed.sent) == 3
    assert delivered.isdisjoint({did for did, _ in resumed.sent})


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


def test_a_delivery_writes_an_event_carrying_no_did(db):
    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    scheduler(db, FakeSource(events=[final()]), Recorder()).tick(utc(2026, 8, 4, 14, 0))

    rows = db.execute(
        "SELECT * FROM events WHERE event_type = 'digest_sent'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["subject"] != DID
    assert DID not in rows[0]["subject"]
    assert len(rows[0]["subject"]) == 64  # sha256 hex


def test_the_same_did_always_hashes_to_the_same_subject(db):
    """Retention cohorts depend on this join surviving deletion."""
    first = repo.subject_for(DID, PEPPER)
    second = repo.subject_for(DID, PEPPER)
    assert first == second
    assert repo.subject_for("did:plc:other", PEPPER) != first


def test_league_health_is_persisted(db):
    from sportscrawl.source import LeagueHealth

    add_subscriber(db)
    repo.add_sub(db, DID, "team", MLB, "100")
    source = FakeSource(events=[final()])
    source.health = {
        MLB: LeagueHealth(
            league=MLB,
            last_fetch_at=utc(2026, 8, 4, 14, 0),
            last_success_at=utc(2026, 8, 4, 14, 0),
            last_event_date=None,
            consecutive_failures=0,
        )
    }

    scheduler(db, source, Recorder()).tick(utc(2026, 8, 4, 14, 0))

    row = repo.get_league_state(db, MLB)
    assert row["consecutive_failures"] == 0
    assert row["last_success_at"] == "2026-08-04T14:00:00Z"
