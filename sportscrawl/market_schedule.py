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


def due_time(day):
    """When the message for this trading day is due, or None if no trading."""
    hours = session(day)
    return hours[1] + CLOSE_DELAY if hours else None


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


def classify_market(row, now: datetime):
    """Where a subscriber sits relative to today's market close."""
    day = new_york_date(now)

    # Already handled today's close for this person (sent, or decided there
    # was nothing to send)? Then nothing to do until the next trading day.
    if row["last_evaluated_date"] == day.isoformat():
        return NOT_DUE, None

    due_at = due_time(day)
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
