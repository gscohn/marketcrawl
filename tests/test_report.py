"""Daily operations report. Spec section 13, liveness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import daily_report
from sportscrawl import repo
from sportscrawl.db import connect, migrate
from sportscrawl.source import LeagueHealth

PEPPER = "test-pepper"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = connect(str(tmp_path / "report.db"))
    migrate(connection)
    return connection


def test_a_quiet_day_reads_as_quiet(db, monkeypatch):
    monkeypatch.setattr(daily_report, "backup_age_hours", lambda *a, **k: 2.0)
    repo.create_subscriber(db, "did:plc:a", NOW, PEPPER, tz="America/Los_Angeles")

    text = daily_report.build(db)
    assert "1 active, 0 paused" in text
    assert "all healthy" in text
    assert "STALE" not in text


def test_a_failing_league_is_named(db, monkeypatch):
    """Only problems are listed. A healthy list every morning trains you to
    skim past the one line that matters."""
    monkeypatch.setattr(daily_report, "backup_age_hours", lambda *a, **k: 2.0)
    repo.save_league_health(db, LeagueHealth(league="soccer/usa.1", last_fetch_at=NOW,
                                             consecutive_failures=4))
    repo.save_league_health(db, LeagueHealth(league="baseball/mlb", last_fetch_at=NOW,
                                             last_success_at=NOW))

    text = daily_report.build(db)
    assert "soccer/usa.1: 4 consecutive failures" in text
    assert "baseball/mlb" not in text


def test_a_missing_backup_is_shouted(db, monkeypatch):
    monkeypatch.setattr(daily_report, "backup_age_hours", lambda *a, **k: None)
    assert "Backup: NONE FOUND" in daily_report.build(db)


def test_a_stale_backup_is_shouted(db, monkeypatch):
    monkeypatch.setattr(daily_report, "backup_age_hours", lambda *a, **k: 80.0)
    assert "STALE" in daily_report.build(db)


def test_churn_appears_only_when_it_happens(db, monkeypatch):
    monkeypatch.setattr(daily_report, "backup_age_hours", lambda *a, **k: 1.0)
    assert "Joined" not in daily_report.build(db)

    repo.create_subscriber(db, "did:plc:b", NOW, PEPPER)
    assert "Joined 1, left 0" in daily_report.build(db)


def test_the_report_fits_in_a_single_dm(db, monkeypatch):
    """The cap is 1000 graphemes, verified against the live API."""
    monkeypatch.setattr(daily_report, "backup_age_hours", lambda *a, **k: 1.0)
    for index in range(40):
        repo.create_subscriber(db, "did:plc:s%d" % index, NOW, PEPPER, tz="UTC")
    for index in range(12):
        repo.save_league_health(db, LeagueHealth(league="league/%d" % index,
                                                 last_fetch_at=NOW,
                                                 consecutive_failures=3))
    assert len(daily_report.build(db)) < 1000


def test_the_report_names_no_subscriber(db, monkeypatch):
    """It is an operations summary, not a subscriber list."""
    monkeypatch.setattr(daily_report, "backup_age_hours", lambda *a, **k: 1.0)
    repo.create_subscriber(db, "did:plc:identifiable", NOW, PEPPER, tz="UTC")
    text = daily_report.build(db)
    assert "did:plc:" not in text
