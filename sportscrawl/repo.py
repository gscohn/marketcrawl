"""Data access.

Dates are stored as YYYY-MM-DD strings and timestamps as ISO-8601 UTC, because
SQLite has no date type and the alternative is guessing at parse time.
"""

from __future__ import annotations

import hmac
import json
from datetime import datetime
from hashlib import sha256

from .models import Sub

ISO = "%Y-%m-%dT%H:%M:%SZ"


def utc_string(when: datetime) -> str:
    return when.strftime(ISO)


def subject_for(did: str, pepper: str) -> str:
    """Pseudonymous identifier for the events table.

    Not reversible to a DID without the pepper; reversible with the database
    and the pepper together, which is why section 12 says pseudonymous rather
    than anonymous. Spec section 4.
    """
    return hmac.new(pepper.encode("utf-8"), did.encode("utf-8"), sha256).hexdigest()


def record_event(connection, when, event_type, did, pepper, league=None, detail=None):
    connection.execute(
        "INSERT INTO events (ts, event_type, subject, league, detail)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            utc_string(when),
            event_type,
            subject_for(did, pepper),
            league,
            json.dumps(detail) if detail is not None else None,
        ),
    )


# -- subscribers ----------------------------------------------------------


def create_subscriber(connection, did, when, pepper, tz=None, send_time="07:00"):
    """First contact. Inert until the config page is saved: no tz and no subs
    means the scheduler skips them twice over. Spec section 9."""
    connection.execute(
        "INSERT OR IGNORE INTO subscribers (did, tz, send_time, state, created_at)"
        " VALUES (?, ?, ?, 'active', ?)",
        (did, tz, send_time, utc_string(when)),
    )
    record_event(connection, when, "signup", did, pepper)


def get_subscriber(connection, did):
    return connection.execute(
        "SELECT * FROM subscribers WHERE did = ?", (did,)
    ).fetchone()


def deliverable_subscribers(connection):
    """Everyone the scheduler might send to this tick.

    The tz filter is here rather than in the caller because a null tz means
    the subscriber never finished setup, and guessing a timezone sends someone
    their digest at 3am.
    """
    return connection.execute(
        "SELECT * FROM subscribers WHERE state = 'active' AND tz IS NOT NULL"
    ).fetchall()


def subs_for(connection, did) -> list:
    rows = connection.execute(
        "SELECT kind, league, team_id FROM subs WHERE did = ?", (did,)
    ).fetchall()
    return [Sub(kind=row["kind"], league=row["league"], team_id=row["team_id"]) for row in rows]


def add_sub(connection, did, kind, league, team_id=None):
    connection.execute(
        "INSERT OR IGNORE INTO subs (did, kind, league, team_id) VALUES (?, ?, ?, ?)",
        (did, kind, league, team_id),
    )


def mark_evaluated(connection, did, local_date):
    connection.execute(
        "UPDATE subscribers SET last_evaluated_date = ? WHERE did = ?",
        (local_date.isoformat(), did),
    )


def mark_sent(connection, did, local_date):
    connection.execute(
        "UPDATE subscribers SET last_sent_date = ?, last_evaluated_date = ?"
        " WHERE did = ?",
        (local_date.isoformat(), local_date.isoformat(), did),
    )


def mark_failed(connection, did, local_date):
    connection.execute(
        "UPDATE subscribers SET last_failure_date = ?, last_evaluated_date = ?"
        " WHERE did = ?",
        (local_date.isoformat(), local_date.isoformat(), did),
    )


def mark_partial(connection, did, local_date):
    """Both dates advance: they got their scores and they got told something
    was missing, so neither a re-send nor a second apology should follow."""
    stamp = local_date.isoformat()
    connection.execute(
        "UPDATE subscribers SET last_sent_date = ?, last_failure_date = ?,"
        " last_evaluated_date = ? WHERE did = ?",
        (stamp, stamp, stamp, did),
    )


# -- league health --------------------------------------------------------


def save_league_health(connection, health):
    connection.execute(
        "INSERT INTO league_state"
        " (league, last_fetch_at, last_success_at, last_event_date, consecutive_failures)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(league) DO UPDATE SET"
        "  last_fetch_at = excluded.last_fetch_at,"
        "  last_success_at = COALESCE(excluded.last_success_at, league_state.last_success_at),"
        "  last_event_date = COALESCE(excluded.last_event_date, league_state.last_event_date),"
        "  consecutive_failures = excluded.consecutive_failures",
        (
            health.league,
            utc_string(health.last_fetch_at) if health.last_fetch_at else None,
            utc_string(health.last_success_at) if health.last_success_at else None,
            health.last_event_date.isoformat() if health.last_event_date else None,
            health.consecutive_failures,
        ),
    )


def get_league_state(connection, league):
    return connection.execute(
        "SELECT * FROM league_state WHERE league = ?", (league,)
    ).fetchone()
