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


# -- conversation state ---------------------------------------------------
#
# Keyed by conversation and holding no DID, so it survives `delete`. A
# returning subscriber's old messages must not replay as fresh commands.


def get_convo_state(connection, convo_id):
    return connection.execute(
        "SELECT * FROM convo_state WHERE convo_id = ?", (convo_id,)
    ).fetchone()


def ensure_convo(connection, convo_id):
    connection.execute(
        "INSERT OR IGNORE INTO convo_state (convo_id) VALUES (?)", (convo_id,)
    )


def set_last_message(connection, convo_id, message_id):
    ensure_convo(connection, convo_id)
    connection.execute(
        "UPDATE convo_state SET last_message_id = ? WHERE convo_id = ?",
        (message_id, convo_id),
    )


def last_message_id(connection, convo_id):
    row = get_convo_state(connection, convo_id)
    return row["last_message_id"] if row else None


def set_help_sent(connection, convo_id, when):
    ensure_convo(connection, convo_id)
    connection.execute(
        "UPDATE convo_state SET last_help_at = ? WHERE convo_id = ?",
        (utc_string(when), convo_id),
    )


def help_sent_at(connection, convo_id):
    row = get_convo_state(connection, convo_id)
    return row["last_help_at"] if row else None


def set_delete_pending(connection, convo_id, until):
    ensure_convo(connection, convo_id)
    connection.execute(
        "UPDATE convo_state SET delete_pending_until = ? WHERE convo_id = ?",
        (utc_string(until) if until else None, convo_id),
    )


def delete_pending_until(connection, convo_id):
    row = get_convo_state(connection, convo_id)
    return row["delete_pending_until"] if row else None


# -- tokens ---------------------------------------------------------------


def issue_token(connection, did, token, scope, expires_at):
    """A fresh token invalidates every earlier one for that DID.

    Spec section 9: `config` issues a fresh link and kills the previous.
    """
    connection.execute(
        "DELETE FROM config_tokens WHERE did = ? AND scope = ?", (did, scope)
    )
    connection.execute(
        "INSERT INTO config_tokens (token, did, scope, expires_at) VALUES (?, ?, ?, ?)",
        (token, did, scope, utc_string(expires_at)),
    )


# -- deletion -------------------------------------------------------------


def delete_subscriber(connection, did, when, pepper):
    """Everything the account record holds, in one transaction.

    convo_state is deliberately untouched: it carries no DID, and dropping it
    would reset the poll cursor so old messages replay as new commands.
    """
    connection.execute("BEGIN")
    try:
        record_event(connection, when, "delete", did, pepper)
        connection.execute("DELETE FROM sessions WHERE did = ?", (did,))
        connection.execute("DELETE FROM config_tokens WHERE did = ?", (did,))
        connection.execute("DELETE FROM subs WHERE did = ?", (did,))
        connection.execute("DELETE FROM subscribers WHERE did = ?", (did,))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def set_state(connection, did, state, when, pepper):
    """Idempotent: re-running `stop` on a paused subscriber changes nothing
    and writes no second event."""
    row = get_subscriber(connection, did)
    if row is None or row["state"] == state:
        return False
    connection.execute(
        "UPDATE subscribers SET state = ?, paused_at = ? WHERE did = ?",
        (state, utc_string(when) if state == "paused" else None, did),
    )
    record_event(connection, when, "pause" if state == "paused" else "resume", did, pepper)
    return True


# -- save confirmations ---------------------------------------------------


def queue_confirmation(connection, did, when):
    """Flag set by the web process, drained by the scheduler.

    The web process does not send: gunicorn runs several workers, a chat login
    per request is slow, and delivery belongs to the scheduler, which already
    owns the sender, the allowlist and per-subscriber error isolation.
    """
    connection.execute(
        "UPDATE subscribers SET confirm_pending_at = ? WHERE did = ?",
        (utc_string(when), did),
    )


def pending_confirmations(connection):
    return connection.execute(
        "SELECT * FROM subscribers WHERE confirm_pending_at IS NOT NULL"
    ).fetchall()


def clear_confirmation(connection, did):
    connection.execute(
        "UPDATE subscribers SET confirm_pending_at = NULL WHERE did = ?", (did,)
    )


def live_token(connection, did, scope, now):
    """An existing token with life left, or None.

    The help reply reuses one rather than minting. issue_token deletes prior
    tokens, so minting on every unrecognised message killed the link the
    subscriber had just been sent: ask for `config`, say "thanks", tap the
    link, get the expired page.
    """
    row = connection.execute(
        "SELECT token FROM config_tokens"
        " WHERE did = ? AND scope = ? AND expires_at > ?"
        " ORDER BY expires_at DESC LIMIT 1",
        (did, scope, utc_string(now)),
    ).fetchone()
    return row["token"] if row else None
