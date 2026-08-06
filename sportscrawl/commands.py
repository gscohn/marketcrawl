"""Inbound command handling.

Six public commands plus one hidden. Spec section 9.

Returns reply text rather than sending it, so every branch is testable without
a network. The poller does the sending.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

from . import repo

TOKEN_TTL = timedelta(minutes=15)
DELETE_WINDOW = timedelta(minutes=5)
HELP_THROTTLE = timedelta(hours=1)

# `stop` is the opt-out path and must work when everything else is broken, so
# it is matched first and accepts synonyms. Spec section 9.
STOP_WORDS = frozenset({"stop", "unsubscribe", "quit", "cancel", "off"})

log = logging.getLogger("sportscrawl.commands")

PUNCTUATION = re.compile(r"[^\w\s]")
WHITESPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Internal spaces survive so `delete confirm` stays two words.
    """
    cleaned = PUNCTUATION.sub(" ", (text or "").lower())
    return WHITESPACE.sub(" ", cleaned).strip()


HELP = """SportsCrawl sends you a daily score digest
for the teams you follow.

Pick teams and set your delivery time:
%s

Commands:
config - get a fresh settings link
stop - pause messages
delete - remove all your data
privacy - privacy policy"""

NUDGE = "Pick teams and set your delivery time here:\n%s"

STOPPED = """Messages paused. You will not get a digest until you send start.

start - resume messages
delete - remove all your data"""

STARTED = "Messages resumed. Your next digest arrives at your usual time."

DELETE_PROMPT = """This removes everything: your team selections, your
delivery time, and your settings links. It cannot be undone.

Send: delete confirm

If you only want the messages to stop, send stop instead. That keeps
your settings."""

DELETED = """Deleted. Nothing of yours is left in the app.

This conversation stays in your Bluesky inbox, because the message thread
is not the app's to delete. Message again any time to start fresh."""

PRIVACY = "Privacy policy:\n%s/privacy"

CONFIG_REPLY = "Pick teams and set your delivery time:\n%s\n\nThe link works for 15 minutes."

ADMIN_REPLY = "Admin:\n%s\n\nThe link works for 15 minutes."


class CommandHandler:
    def __init__(self, connection, pepper, base_url, admin_did=None):
        self._db = connection
        self._pepper = pepper
        self._base = base_url.rstrip("/")
        self._admin_did = admin_did

    # -- helpers ----------------------------------------------------------

    def _config_link(self, did, now, scope="config"):
        """A fresh link. Invalidates any previous one, per section 9."""
        token = secrets.token_urlsafe(32)
        repo.issue_token(self._db, did, token, scope, now + TOKEN_TTL)
        return "%s/c/%s" % (self._base, token)

    def _reusable_link(self, did, now):
        """A link for the help reply, reusing a live token when there is one.

        `config` is an explicit request and always mints fresh. Help is not:
        someone who asks for a link and then says "thanks" would otherwise have
        the link they were just sent invalidated by their own pleasantry.
        """
        existing = repo.live_token(self._db, did, "config", now)
        if existing:
            return "%s/c/%s" % (self._base, existing)
        return self._config_link(did, now)

    def _help_reply(self, convo_id, did, now):
        """First unknown message gets full help; a repeat inside the hour gets
        a short nudge.

        Throttled in the database, not in memory: an unthrottled help reply to
        another bot is an infinite loop that burns the chat rate limit, and a
        loop like that causes exactly the restarts that would clear an
        in-memory counter.
        """
        previous = repo.help_sent_at(self._db, convo_id)
        link = self._reusable_link(did, now)
        repo.set_help_sent(self._db, convo_id, now)

        if previous and previous > repo.utc_string(now - HELP_THROTTLE):
            return NUDGE % link
        return HELP % link

    @staticmethod
    def _match_name(text: str) -> str:
        if text in ("delete confirm", "confirm delete", "confirm"):
            return "delete_confirm"
        if text in STOP_WORDS:
            return "stop"
        if text in ("start", "delete", "config", "privacy", "admin"):
            return text
        return "unknown"

    # -- dispatch ---------------------------------------------------------

    def handle(self, message, now: datetime) -> str:
        did = message.sender_did
        convo_id = message.convo_id
        text = normalize(message.text)

        # The privacy policy promises inbound DMs are processed for commands
        # and not retained. This is the line that has to keep that true: the
        # DID and the matched command, never the message body. `unknown`
        # covers everything unmatched, so no user text reaches a log file.
        log.info("command did=%s matched=%s", did, self._match_name(text))

        # Any inbound message from an unknown DID creates the subscriber
        # before the command is dispatched. Inert until the config page is
        # saved: no tz and no subs means the scheduler skips them twice over.
        existing = repo.get_subscriber(self._db, did)
        if existing is None:
            repo.create_subscriber(self._db, did, now, self._pepper)

        pending = repo.delete_pending_until(self._db, convo_id)
        pending_live = bool(pending) and pending > repo.utc_string(now)

        # `delete confirm` before `delete`, and both before anything that
        # could swallow the word.
        if text in ("delete confirm", "confirm delete", "confirm"):
            if not pending_live:
                repo.set_delete_pending(self._db, convo_id, now + DELETE_WINDOW)
                return DELETE_PROMPT
            repo.set_delete_pending(self._db, convo_id, None)
            # Idempotent: confirming twice, or after the row is already gone,
            # is a no-op rather than an error.
            if repo.get_subscriber(self._db, did) is not None:
                repo.delete_subscriber(self._db, did, now, self._pepper)
            return DELETED

        # Any other command cancels a pending delete: someone who types
        # `stop` after `delete` has changed their mind.
        if pending_live:
            repo.set_delete_pending(self._db, convo_id, None)

        if text in STOP_WORDS:
            repo.set_state(self._db, did, "paused", now, self._pepper)
            return STOPPED

        if text == "start":
            repo.set_state(self._db, did, "active", now, self._pepper)
            return STARTED

        if text == "delete":
            repo.set_delete_pending(self._db, convo_id, now + DELETE_WINDOW)
            return DELETE_PROMPT

        if text == "config":
            return CONFIG_REPLY % self._config_link(did, now)

        if text == "privacy":
            return PRIVACY % self._base

        if text == "admin":
            # Hidden. From any other DID this is an unknown word and falls
            # through to help: no error, no "not authorized", no
            # acknowledgement that the word means anything. A denial is
            # itself an answer.
            if self._admin_did and did == self._admin_did:
                repo.record_event(self._db, now, "admin_link", did, self._pepper)
                return ADMIN_REPLY % self._config_link(did, now, scope="admin")

        return self._help_reply(convo_id, did, now)
