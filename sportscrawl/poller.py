"""Inbound DM poll loop.

listConvos reports each conversation's latest message; it is not a queue and
it does not remember what the bot has acted on. Without a cursor a restart
re-reads recent history and re-executes commands. Spec section 8.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from . import repo
from .chat import parse_messages


@dataclass
class PollReport:
    conversations: int = 0
    handled: int = 0
    skipped: int = 0

    def __str__(self) -> str:
        return "conversations=%d handled=%d skipped=%d" % (
            self.conversations,
            self.handled,
            self.skipped,
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
        convos = self._client.list_convos().get("convos", [])

        for convo in convos:
            report.conversations += 1
            if not convo.get("unreadCount"):
                continue
            self._drain(convo["id"], now, report)

        return report

    def _drain(self, convo_id: str, now, report) -> None:
        cursor = repo.last_message_id(self._db, convo_id)
        payload = self._client.get_messages(convo_id)
        messages = parse_messages(payload, convo_id)

        # getMessages returns newest first; commands have to run in the order
        # they were sent or `delete` and `delete confirm` arrive backwards.
        messages.sort(key=lambda m: m.message_id)

        newest = cursor
        for message in messages:
            if cursor is not None and message.message_id <= cursor:
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
