"""Command handling tests. Spec sections 8 and 9."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sportscrawl import repo
from sportscrawl.chat import Message
from sportscrawl.commands import CommandHandler, normalize
from sportscrawl.db import connect, migrate
from sportscrawl.poller import Poller

PEPPER = "test-pepper"
BASE = "https://sportscrawl.app"
DID = "did:plc:stranger"
ADMIN = "did:plc:7yoku6ecesrllopmfylrvflp"
CONVO = "convo-1"

NOW = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    connection = connect(str(tmp_path / "cmd.db"))
    migrate(connection)
    return connection


@pytest.fixture
def handler(db):
    return CommandHandler(db, PEPPER, BASE, admin_did=ADMIN)


def msg(text, did=DID, convo=CONVO, message_id="m1"):
    return Message(message_id=message_id, convo_id=convo, sender_did=did,
                   text=text, sent_at="")


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["STOP", " stop ", "stop.", "Stop!", "“stop”"])
def test_stop_is_recognised_however_it_is_typed(raw):
    assert normalize(raw) == "stop"


def test_normalisation_keeps_internal_spaces():
    assert normalize("Delete Confirm!") == "delete confirm"


# --------------------------------------------------------------------------
# First contact
# --------------------------------------------------------------------------


def test_first_message_creates_a_subscriber_and_returns_help(db, handler):
    reply = handler.handle(msg("hello"), NOW)

    row = repo.get_subscriber(db, DID)
    assert row is not None
    assert row["state"] == "active"
    assert row["tz"] is None          # inert until the config page is saved
    assert row["send_time"] == "07:00"
    assert "Pick teams" in reply
    assert "/c/" in reply


def test_the_help_reply_always_carries_a_config_link(db, handler):
    """Otherwise someone gets help and still cannot pick teams."""
    reply = handler.handle(msg("what is this"), NOW)
    assert "%s/c/" % BASE in reply


def test_a_signup_event_is_written_once(db, handler):
    handler.handle(msg("hello"), NOW)
    handler.handle(msg("hello again", message_id="m2"), NOW)
    count = db.execute(
        "SELECT COUNT(*) c FROM events WHERE event_type = 'signup'"
    ).fetchone()["c"]
    assert count == 1


# --------------------------------------------------------------------------
# Help throttling
# --------------------------------------------------------------------------


def test_a_repeat_unknown_message_gets_a_short_nudge(db, handler):
    first = handler.handle(msg("hello"), NOW)
    second = handler.handle(msg("hi there", message_id="m2"), NOW + timedelta(minutes=1))

    assert "Commands:" in first
    assert "Commands:" not in second
    assert "/c/" in second


def test_full_help_returns_after_the_throttle_expires(db, handler):
    handler.handle(msg("hello"), NOW)
    later = handler.handle(msg("hello", message_id="m2"), NOW + timedelta(hours=2))
    assert "Commands:" in later


def test_a_bot_loop_cannot_produce_endless_help(db, handler):
    """An unthrottled help reply to another bot burns the chat rate limit."""
    replies = [
        handler.handle(msg("???", message_id="m%d" % i), NOW + timedelta(seconds=i))
        for i in range(20)
    ]
    full = [r for r in replies if "Commands:" in r]
    assert len(full) == 1


# --------------------------------------------------------------------------
# stop and start
# --------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["stop", "unsubscribe", "quit", "cancel", "off"])
def test_every_stop_synonym_pauses(db, handler, word):
    handler.handle(msg("hello"), NOW)
    reply = handler.handle(msg(word, message_id="m2"), NOW)

    assert repo.get_subscriber(db, DID)["state"] == "paused"
    assert "start" in reply and "delete" in reply


def test_stop_is_idempotent(db, handler):
    handler.handle(msg("hello"), NOW)
    handler.handle(msg("stop", message_id="m2"), NOW)
    handler.handle(msg("stop", message_id="m3"), NOW + timedelta(minutes=1))

    pauses = db.execute(
        "SELECT COUNT(*) c FROM events WHERE event_type = 'pause'"
    ).fetchone()["c"]
    assert pauses == 1


def test_start_resumes(db, handler):
    handler.handle(msg("stop"), NOW)
    handler.handle(msg("start", message_id="m2"), NOW)
    assert repo.get_subscriber(db, DID)["state"] == "active"


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


def test_delete_never_deletes_on_the_first_message(db, handler):
    reply = handler.handle(msg("delete"), NOW)
    assert repo.get_subscriber(db, DID) is not None
    assert "delete confirm" in reply


def test_delete_confirm_removes_everything(db, handler):
    handler.handle(msg("hello"), NOW)
    repo.add_sub(db, DID, "team", "baseball/mlb", "19")
    handler.handle(msg("delete", message_id="m2"), NOW)
    reply = handler.handle(msg("delete confirm", message_id="m3"), NOW)

    assert repo.get_subscriber(db, DID) is None
    assert repo.subs_for(db, DID) == []
    assert db.execute("SELECT COUNT(*) c FROM config_tokens").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0
    assert "Deleted" in reply


def test_a_delete_event_survives_the_deletion(db, handler):
    """It is the only record that someone left, and churn is the metric the
    project exists to gather."""
    handler.handle(msg("delete"), NOW)
    handler.handle(msg("delete confirm", message_id="m2"), NOW)

    events = db.execute(
        "SELECT event_type FROM events ORDER BY id"
    ).fetchall()
    assert "delete" in [e["event_type"] for e in events]


def test_delete_confirm_outside_the_window_re_prompts(db, handler):
    handler.handle(msg("delete"), NOW)
    reply = handler.handle(msg("delete confirm", message_id="m2"), NOW + timedelta(minutes=6))

    assert repo.get_subscriber(db, DID) is not None
    assert "delete confirm" in reply


def test_another_command_cancels_a_pending_delete(db, handler):
    """Someone who types stop after delete has changed their mind."""
    handler.handle(msg("delete"), NOW)
    handler.handle(msg("stop", message_id="m2"), NOW)
    handler.handle(msg("delete confirm", message_id="m3"), NOW)

    assert repo.get_subscriber(db, DID) is not None


def test_delete_confirm_twice_is_a_no_op(db, handler):
    handler.handle(msg("delete"), NOW)
    handler.handle(msg("delete confirm", message_id="m2"), NOW)
    reply = handler.handle(msg("delete confirm", message_id="m3"), NOW)
    assert "delete confirm" in reply or "Deleted" in reply


def test_a_returning_did_is_a_brand_new_subscriber(db, handler):
    """The path most likely to rot silently, and the one where a bug breaks a
    published promise. Spec section 9."""
    handler.handle(msg("hello"), NOW)
    repo.add_sub(db, DID, "team", "baseball/mlb", "19")
    handler.handle(msg("delete", message_id="m2"), NOW)
    handler.handle(msg("delete confirm", message_id="m3"), NOW)

    handler.handle(msg("hello", message_id="m4"), NOW + timedelta(days=30))

    row = repo.get_subscriber(db, DID)
    assert row is not None
    assert row["state"] == "active"
    assert row["tz"] is None
    assert repo.subs_for(db, DID) == []          # no history carried over
    assert row["last_sent_date"] is None


# --------------------------------------------------------------------------
# config, privacy, admin
# --------------------------------------------------------------------------


def test_config_issues_a_token_and_invalidates_the_previous(db, handler):
    handler.handle(msg("config"), NOW)
    first = db.execute("SELECT token FROM config_tokens").fetchone()["token"]

    handler.handle(msg("config", message_id="m2"), NOW + timedelta(minutes=1))
    rows = db.execute("SELECT token FROM config_tokens WHERE scope='config'").fetchall()

    assert len(rows) == 1
    assert rows[0]["token"] != first


def test_privacy_returns_the_policy_url(db, handler):
    assert handler.handle(msg("privacy"), NOW) == "Privacy policy:\n%s/privacy" % BASE


def test_admin_from_the_operator_returns_an_admin_link(db, handler):
    reply = handler.handle(msg("admin", did=ADMIN), NOW)
    assert "Admin" in reply
    scope = db.execute(
        "SELECT scope FROM config_tokens WHERE did = ?", (ADMIN,)
    ).fetchone()["scope"]
    assert scope == "admin"


def test_admin_from_anyone_else_looks_like_an_unknown_word(db, handler):
    """No error, no 'not authorized', no acknowledgement the word means
    anything. A denial is itself an answer."""
    reply = handler.handle(msg("admin", did=DID), NOW)
    assert "Admin" not in reply
    assert "Commands:" in reply
    assert db.execute(
        "SELECT COUNT(*) c FROM config_tokens WHERE scope = 'admin'"
    ).fetchone()["c"] == 0


# --------------------------------------------------------------------------
# The poll loop
# --------------------------------------------------------------------------


class FakeClient:
    def __init__(self, convos, messages):
        self.convos = convos
        self.messages = messages
        self.sent = []
        self.read = []

    def list_convos(self, **kwargs):
        return {"convos": self.convos}

    def get_messages(self, convo_id, **kwargs):
        return {"messages": self.messages.get(convo_id, [])}

    def send_message(self, convo_id, text):
        self.sent.append((convo_id, text))
        return {"id": "reply"}

    def update_read(self, convo_id, message_id=None):
        self.read.append((convo_id, message_id))


def raw(message_id, text, did=DID):
    return {"id": message_id, "text": text, "sender": {"did": did}}


def test_the_poll_loop_answers_a_new_message(db, handler):
    client = FakeClient(
        [{"id": CONVO, "unreadCount": 1}],
        {CONVO: [raw("m1", "hello")]},
    )
    report = Poller(db, client, handler).poll(NOW)

    assert report.handled == 1
    assert len(client.sent) == 1
    assert repo.last_message_id(db, CONVO) == "m1"


def test_a_restart_does_not_re_execute_handled_commands(db, handler):
    """Without a cursor a restart re-reads history and re-runs commands:
    duplicate help at best, a replayed delete confirm at worst."""
    client = FakeClient(
        [{"id": CONVO, "unreadCount": 1}],
        {CONVO: [raw("m1", "hello")]},
    )
    Poller(db, client, handler).poll(NOW)
    Poller(db, client, handler).poll(NOW + timedelta(minutes=1))

    assert len(client.sent) == 1


def test_messages_are_processed_oldest_first(db, handler):
    """getMessages returns newest first. Out of order, delete and delete
    confirm arrive backwards and the confirmation lands on nothing."""
    client = FakeClient(
        [{"id": CONVO, "unreadCount": 2}],
        {CONVO: [raw("m2", "delete confirm"), raw("m1", "delete")]},
    )
    repo.create_subscriber(db, DID, NOW, PEPPER)
    Poller(db, client, handler).poll(NOW)

    assert repo.get_subscriber(db, DID) is None
    assert "Deleted" in client.sent[-1][1]


def test_the_bots_own_messages_are_ignored(db, handler):
    """Replies come back in the same thread; handling them is a loop."""
    bot = "did:plc:kmx5rkipob7xh6lhgxzxxkxq"
    client = FakeClient(
        [{"id": CONVO, "unreadCount": 1}],
        {CONVO: [raw("m1", "Sportscrawl sends you a daily digest", did=bot)]},
    )
    report = Poller(db, client, handler, bot_did=bot).poll(NOW)

    assert report.handled == 0
    assert client.sent == []


def test_conversations_with_nothing_unread_are_skipped(db, handler):
    client = FakeClient(
        [{"id": CONVO, "unreadCount": 0}],
        {CONVO: [raw("m1", "hello")]},
    )
    report = Poller(db, client, handler).poll(NOW)
    assert report.handled == 0
    assert client.sent == []


def test_the_cursor_advances_past_our_own_replies(db, handler):
    """Deciding to ignore a message is processing it. Leaving the cursor
    behind means re-reading every reply ever sent, on every poll."""
    bot = "did:plc:bot"
    client = FakeClient(
        [{"id": CONVO, "unreadCount": 1}],
        {CONVO: [raw("m1", "hello"), raw("m2", "here is help", did=bot)]},
    )
    Poller(db, client, handler, bot_did=bot).poll(NOW)
    assert repo.last_message_id(db, CONVO) == "m2"

    client.messages[CONVO].append(raw("m3", "config"))
    report = Poller(db, client, handler, bot_did=bot).poll(NOW)
    assert report.skipped == 0        # m2 is behind the cursor now
    assert report.handled == 1
