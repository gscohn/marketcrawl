"""Inbound DM poll loop.

listConvos reports each conversation's latest message; it is not a queue and
it does not remember what the bot has acted on. Without a cursor a restart
re-reads recent history and re-executes commands. Spec section 8.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import repo
from .chat import parse_messages

# How old a message can be and still be acted on when a conversation has no
# stored cursor. New subscribers always message within seconds of arriving, so
# this only ever suppresses backlog.
ADOPTION_WINDOW = timedelta(hours=1)

# Safety valve on pagination. listConvos appears to return most-recent first,
# so in practice the conversations with unread messages surface early, but that
# ordering is an observation rather than a documented guarantee. Paging until
# the cursor runs out removes the assumption; the cap stops a bad cursor
# spinning forever.
MAX_PAGES = 20


def _older_than(sent_at: str, now: datetime, window: timedelta) -> bool:
    """True when a timestamp is definitely older than the window.

    An unparseable or missing timestamp returns False: acting on a message is
    the safer default than silently swallowing a real command.
    """
    if not sent_at:
        return False
    try:
        cleaned = sent_at.replace("Z", "+00:00")
        stamp = datetime.fromisoformat(cleaned)
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp < now - window


@dataclass
class PollReport:
    conversations: int = 0
    handled: int = 0
    skipped: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return "conversations=%d handled=%d skipped=%d errors=%d" % (
            self.conversations,
            self.handled,
            self.skipped,
            self.errors,
        )


class Poller:
    def __init__(self, connection, client, handler, bot_did=None):
        self._db = connection
        self._client = client
        self._handler = handler
        self._bot_did = bot_did

    def poll(self, now=None) -> PollReport:
        now = now or datetime.now(timezone.utc)
        report = PollReport()

        # No status filter. The default view includes message requests, and
        # adding status="accepted" is exactly the change that would make every
        # new signup invisible. Verified 2026-08-04, spec section 8.
        cursor = None
        for _ in range(MAX_PAGES):
            page = self._client.list_convos(cursor=cursor)
            convos = page.get("convos", [])

            for convo in convos:
                report.conversations += 1
                if not convo.get("unreadCount"):
                    continue
                try:
                    self._drain(convo["id"], now, report)
                except Exception as exc:
                    # One broken conversation must not starve the rest. The
                    # cursor is not advanced, so the same messages are retried
                    # next poll.
                    report.errors += 1
                    print("poll failed for convo %s: %s" % (convo["id"], exc),
                          flush=True)

            cursor = page.get("cursor")
            if not cursor or not convos:
                break

        return report

    def _drain(self, convo_id: str, now, report) -> None:
        cursor = repo.last_message_id(self._db, convo_id)
        messages = self._unread(convo_id, cursor)

        # getMessages returns newest first; commands have to run in the order
        # they were sent or `delete` and `delete confirm` arrive backwards.
        messages.sort(key=lambda m: m.message_id)

        newest = cursor
        for message in messages:
            if cursor is not None and message.message_id <= cursor:
                continue

            if cursor is None and _older_than(message.sent_at, now, ADOPTION_WINDOW):
                # First sight of this conversation and the message predates
                # us. Adopt it silently instead of acting on it.
                #
                # A fresh database against an existing thread would otherwise
                # replay the entire history as new commands: duplicate help at
                # best, an old `delete confirm` executing at worst. Observed on
                # the first production deploy, which re-answered a day-old
                # message.
                #
                # This only applies with no cursor at all. An outage leaves a
                # cursor behind, so a backlog after downtime is still handled.
                repo.set_last_message(self._db, convo_id, message.message_id)
                newest = message.message_id
                report.skipped += 1
                continue
            if self._bot_did and message.sender_did == self._bot_did:
                # Our own replies come back in the same thread. Handling them
                # would be a self-sustaining loop.
                #
                # The cursor still advances past them. Deciding to ignore a
                # message is processing it, and leaving the cursor behind means
                # re-reading every reply we have ever sent on each poll.
                repo.set_last_message(self._db, convo_id, message.message_id)
                newest = message.message_id
                report.skipped += 1
                continue

            reply = self._handler.handle(message, now)

            # Commit the cursor in the same step as the reply. A crash between
            # them re-runs one command, which is why every branch is
            # idempotent.
            if reply:
                self._client.send_message(convo_id, reply)
            repo.set_last_message(self._db, convo_id, message.message_id)
            newest = message.message_id
            report.handled += 1

        if newest:
            self._client.update_read(convo_id, newest)

    def _unread(self, convo_id: str, cursor):
        """Every message newer than the cursor, following pages backwards.

        getMessages returns newest first, one page at a time. Reading only the
        first page silently drops commands whenever more than a page arrives
        between polls, and the cursor then jumps past them.
        """
        collected = []
        page_cursor = None

        for _ in range(MAX_PAGES):
            payload = self._client.get_messages(convo_id, cursor=page_cursor)
            batch = parse_messages(payload, convo_id)
            if not batch:
                break

            collected.extend(batch)

            # Stop as soon as this page reaches something already processed.
            if cursor is not None and any(m.message_id <= cursor for m in batch):
                break

            page_cursor = payload.get("cursor")
            if not page_cursor:
                break

        return collected
