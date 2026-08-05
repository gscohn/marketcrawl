"""Save confirmation. The message that stops a fifteen hour silence reading
like a broken product."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sportscrawl.confirm import confirmation_text, next_delivery

PACIFIC = "America/Los_Angeles"


def utc(y, m, d, h, minute=0):
    return datetime(y, m, d, h, minute, tzinfo=timezone.utc)


def test_a_save_before_the_send_time_promises_today():
    when, day = next_delivery("17:00", PACIFIC, utc(2026, 8, 5, 18, 0))  # 11:00 local
    assert day == "today"
    assert when.hour == 17


def test_a_save_after_the_send_time_promises_tomorrow():
    """The case this exists for: configure at 2pm for a 5:30am delivery."""
    when, day = next_delivery("05:30", PACIFIC, utc(2026, 8, 5, 21, 0))  # 14:00 local
    assert day == "tomorrow"
    assert when.day == 6


def test_a_save_before_the_send_time_but_already_sent_promises_tomorrow():
    """Editing settings after the morning digest has already gone."""
    when, day = next_delivery("05:30", PACIFIC, utc(2026, 8, 5, 13, 0),
                              already_sent_today=True)
    assert day == "tomorrow"


def test_the_message_states_the_count_and_the_time():
    text = confirmation_text("05:30", PACIFIC, 9, 1, utc(2026, 8, 5, 21, 0))
    assert "9 teams and 1 competition" in text
    assert "5:30am" in text
    assert "tomorrow at 5:30am" in text


def test_singular_and_plural_read_correctly():
    text = confirmation_text("07:00", PACIFIC, 1, 0, utc(2026, 8, 5, 21, 0))
    assert "1 team," in text
    assert "teams" not in text.split("delivered")[0]


def test_quiet_days_are_explained():
    """Otherwise the first silent day looks like a failure."""
    text = confirmation_text("07:00", PACIFIC, 3, 0, utc(2026, 8, 5, 21, 0))
    assert "Quiet days are normal" in text


def test_picking_nothing_promises_nothing():
    """Saying a digest is coming to someone who chose no teams would be a
    promise the scheduler will not keep."""
    text = confirmation_text("07:00", PACIFIC, 0, 0, utc(2026, 8, 5, 21, 0))
    assert "not picked any teams" in text
    assert "arrives" not in text


def test_the_confirmation_fits_in_a_dm():
    text = confirmation_text("05:30", PACIFIC, 40, 5, utc(2026, 8, 5, 21, 0))
    assert len(text) < 1000
