"""Market calendar tests. Is the shop open, and when does it close?"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from sportscrawl.nyse_calendar import CalendarExpired, new_york_date, session


def utc(*parts):
    return datetime(*parts, tzinfo=timezone.utc)


def test_a_normal_day_closes_at_four_new_york_time():
    # October is summer time (EDT, UTC-4): 4:00pm in New York is 20:00 UTC.
    opens, closes = session(date(2026, 10, 30))
    assert opens == utc(2026, 10, 30, 13, 30)
    assert closes == utc(2026, 10, 30, 20, 0)


def test_winter_time_moves_the_close_an_hour_later_in_utc():
    # Clocks go back on Nov 1, 2026. 4:00pm EST is 21:00 UTC.
    _, closes = session(date(2026, 11, 2))
    assert closes == utc(2026, 11, 2, 21, 0)


def test_day_after_thanksgiving_closes_at_one():
    _, closes = session(date(2026, 11, 27))
    assert closes == utc(2026, 11, 27, 18, 0)


def test_christmas_eve_closes_early():
    _, closes = session(date(2026, 12, 24))
    assert closes == utc(2026, 12, 24, 18, 0)


@pytest.mark.parametrize("day", [
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 7, 3),    # Independence Day observed on a Friday
    date(2027, 12, 24),  # Christmas observed on a Friday
])
def test_holidays_are_closed(day):
    assert session(day) is None


@pytest.mark.parametrize("day", [date(2026, 10, 31), date(2026, 11, 1)])
def test_weekends_are_closed(day):
    assert session(day) is None


def test_a_year_nobody_typed_in_fails_loudly():
    with pytest.raises(CalendarExpired):
        session(date(2028, 1, 3))


def test_late_evening_in_california_is_already_tomorrow_in_new_york():
    # 10pm Pacific on Oct 30 is 05:00 UTC on Oct 31, which is 1am in New York.
    assert new_york_date(utc(2026, 10, 31, 5, 0)) == date(2026, 10, 31)
    assert new_york_date(utc(2026, 10, 31, 3, 0)) == date(2026, 10, 30)
