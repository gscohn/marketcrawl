"""Deciding whether a subscriber's market message is due.

ELI5: the original scheduler wakes up every minute and asks, for each
subscriber, "is it their send time yet?" Sports fans each pick their own time
(7:00am, say). Market subscribers don't: everyone's message goes out a few
minutes after the stock market closes. So this file answers the same question
with a different rule:

    due when (today's closing bell) + CLOSE_DELAY has passed

and hands back the exact same kind of answer (NOT_DUE / DUE / EXPIRED plus a
Candidate) as the original `scheduler.classify`, so the scheduler can use it
without knowing the difference.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .nyse_calendar import NEW_YORK, new_york_date, previous_session_day, session
from .scheduler import DUE, EXPIRED, LATENESS_WINDOW, NOT_DUE, Candidate

# Wait this long after the bell before sending. The closing price is set by
# a closing auction that takes a few minutes to print, and free data feeds
# can lag a little behind that. Fifteen minutes is comfortably past both.
CLOSE_DELAY = timedelta(minutes=15)

# The public earnings post waits longer. Most big companies put out their
# results just after the bell (4:05, 4:30...), well after the price DMs have
# gone, so waiting an hour and a quarter catches nearly all of them.
EARNINGS_DELAY = timedelta(minutes=75)

# The operator's daily summary comes last, once everything else for the day
# (the DMs, the price post, the earnings post) has had its chance to go.
REPORT_DELAY = timedelta(minutes=90)


def due_time(day, delay=CLOSE_DELAY):
    """When the message for this trading day is due, or None if no trading.
    `delay` is how long after the closing bell (the DMs' 15 minutes unless
    told otherwise)."""
    hours = session(day)
    return hours[1] + delay if hours else None


def earnings_due(day):
    return due_time(day, EARNINGS_DELAY)


def report_due(day):
    return due_time(day, REPORT_DELAY)


def earnings_window(day):
    """Which earnings releases belong in this day's public earnings post:
    (start, end]. Same idea as filing_window below, on the earnings post's
    later clock: from yesterday's post to today's, so a company that reports
    before the open (7am, say) is in that evening's post, and one that
    reports after the close is too."""
    end = earnings_due(day)
    if end is None:
        return None
    return earnings_due(previous_session_day(day)), end


def filing_window(day):
    """Which filings belong in this day's message: (start, end].

    ELI5: everything that arrived after yesterday's message was due, up to
    when today's is due. Lined up end to end, the windows never overlap and
    never leave a gap, so each filing is mentioned exactly once. Earnings
    released after Tuesday's message show up in Wednesday's, which is also
    the day the price reacts to them.
    """
    end = due_time(day)
    if end is None:
        return None
    return due_time(previous_session_day(day)), end


def classify_market(row, now: datetime, due=due_time):
    """Where a subscriber sits relative to today's market close.

    `due` says when, given the day: the DMs' time unless told otherwise
    (the public earnings post passes its own, later, time)."""
    day = new_york_date(now)

    # Already handled today's close for this person (sent, or decided there
    # was nothing to send)? Then nothing to do until the next trading day.
    if row["last_evaluated_date"] == day.isoformat():
        return NOT_DUE, None

    due_at = due(day)
    if due_at is None:
        # Weekend or holiday: there's no close, so there's no message. Not
        # an apology, not a "markets closed today" note; just quiet, the
        # same way the sports version says nothing on a day with no games.
        return NOT_DUE, None

    lateness = now - due_at

    candidate = Candidate(
        did=row["did"],
        tz=row["tz"],
        local_now=now.astimezone(NEW_YORK),
        local_date=day,
        lateness=lateness,
    )

    if lateness < timedelta(0):
        return NOT_DUE, candidate
    # The original rule, kept: if we're more than 90 minutes late (the
    # computer was off, say), skip the day. A "today's close" message that
    # arrives at midnight is worse than none.
    if lateness > LATENESS_WINDOW:
        return EXPIRED, candidate
    return DUE, candidate
