"""When is the New York Stock Exchange open?

ELI5: the stock market is like a shop with fixed hours. It opens at 9:30 in
the morning and closes at 4:00 in the afternoon, New York time, on weekdays.
On holidays the shop stays shut all day, and on a few special days (the day
after Thanksgiving, Christmas Eve) it closes early, at 1:00.

marketcrawl sends its message a little after the shop closes, so it has to
know what time "closing" is on any given day. That is all this file answers.

The dates are typed in by hand from the NYSE's own published calendar
(nyse.com/markets/hours-calendars), not computed from rules, because the
exchange decides them and occasionally surprises everyone (it closed for a
national day of mourning in 2025). A library could do this, but the good ones
need pandas, and this whole project has one dependency.

Pure: no network, no database, no clock. Pass in a date, get an answer.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Every time in this file is New York time. zoneinfo handles the switch
# between summer time (EDT, UTC-4) and winter time (EST, UTC-5) for us, so a
# 4:00pm close is 20:00 UTC in October and 21:00 UTC in December without any
# arithmetic of our own.
NEW_YORK = ZoneInfo("America/New_York")

OPEN = time(9, 30)
CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Days the shop is shut all day. Weekends are not listed: they are always shut.
HOLIDAYS = frozenset({
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King, Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day, observed (the 4th is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # Martin Luther King, Jr. Day
    date(2027, 2, 15),   # Washington's Birthday
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth, observed (the 19th is a Saturday)
    date(2027, 7, 5),    # Independence Day, observed (the 4th is a Sunday)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas, observed (the 25th is a Saturday)
})

# Days the shop opens as normal but closes at 1:00pm.
EARLY_CLOSES = frozenset({
    date(2026, 11, 27),  # day after Thanksgiving
    date(2026, 12, 24),  # Christmas Eve
    date(2027, 11, 26),  # day after Thanksgiving
})

# The last year the tables above cover. The NYSE publishes about two years
# ahead; when it publishes the next one, add its dates and bump this.
KNOWN_THROUGH = 2027


class CalendarExpired(Exception):
    """We were asked about a year nobody has typed in yet.

    ELI5: rather than guess and maybe send a "market closed" message on a
    normal trading day (or skip a real one), we stop and shout. The fix is to
    add next year's dates above.
    """


def session(day: date):
    """The open and close for one day, as UTC datetimes, or None if shut.

    ELI5: hand it a calendar date, it tells you "open from X until Y", or
    "closed today".
    """
    if day.year > KNOWN_THROUGH:
        raise CalendarExpired(
            "NYSE calendar only covers through %d; add %d's holidays"
            % (KNOWN_THROUGH, day.year)
        )

    # Monday is 0 and Sunday is 6, so 5 and 6 are the weekend.
    if day.weekday() >= 5 or day in HOLIDAYS:
        return None

    closing = EARLY_CLOSE if day in EARLY_CLOSES else CLOSE
    opens = datetime.combine(day, OPEN, tzinfo=NEW_YORK)
    closes = datetime.combine(day, closing, tzinfo=NEW_YORK)
    return opens.astimezone(ZoneInfo("UTC")), closes.astimezone(ZoneInfo("UTC"))


def previous_session_day(day: date) -> date:
    """The last trading day before `day`. Monday's is the Friday before
    (or Thursday, if that Friday was a holiday)."""
    day -= timedelta(days=1)
    while session(day) is None:
        day -= timedelta(days=1)
    return day


def new_york_date(when: datetime) -> date:
    """What day is it in New York at this moment?

    ELI5: at 11pm in California it is already tomorrow in New York. The
    market runs on New York's calendar, so that is the one that counts.
    """
    return when.astimezone(NEW_YORK).date()
