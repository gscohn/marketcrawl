"""When is a market message due?"""

from __future__ import annotations

from datetime import datetime, timezone

from sportscrawl.market_schedule import classify_market
from sportscrawl.scheduler import DUE, EXPIRED, NOT_DUE


def row(last_evaluated=None):
    return {"did": "did:plc:x", "tz": "America/New_York", "send_time": "07:00",
            "last_evaluated_date": last_evaluated}


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def test_not_due_before_the_close_plus_delay():
    # 4:10pm New York: the bell has rung but the delay hasn't passed.
    assert classify_market(row(), utc(2026, 10, 30, 20, 10))[0] == NOT_DUE


def test_due_fifteen_minutes_after_the_close():
    verdict, candidate = classify_market(row(), utc(2026, 10, 30, 20, 15))
    assert verdict == DUE
    assert candidate.local_date.isoformat() == "2026-10-30"


def test_due_at_quarter_past_one_on_an_early_close():
    # Day after Thanksgiving, winter time: 1:15pm EST is 18:15 UTC.
    assert classify_market(row(), utc(2026, 11, 27, 18, 15))[0] == DUE
    assert classify_market(row(), utc(2026, 11, 27, 18, 0))[0] == NOT_DUE


def test_never_due_on_a_holiday():
    assert classify_market(row(), utc(2026, 11, 26, 21, 30))[0] == NOT_DUE


def test_never_due_on_a_weekend():
    assert classify_market(row(), utc(2026, 10, 31, 20, 30))[0] == NOT_DUE


def test_expired_when_far_too_late():
    # 6:00pm New York: an hour and three quarters after due.
    assert classify_market(row(), utc(2026, 10, 30, 22, 0))[0] == EXPIRED


def test_once_per_session():
    verdict, _ = classify_market(row("2026-10-30"), utc(2026, 10, 30, 20, 30))
    assert verdict == NOT_DUE
