"""Delivery targets for the scheduler.

The scheduler takes a sender callable, so stdout, chat, and a restricted chat
sender are interchangeable. Build step 3 used stdout; step 4 sends for real,
to one DID only. Spec section 14.
"""

from __future__ import annotations


class ChatSender:
    """Sends a digest as a Bluesky DM.

    Conversation ids are cached per DID. getConvoForMembers is idempotent and
    returns the existing conversation when there is one, which there always
    will be: subscribers have to message the bot first to sign up, and that is
    what creates the conversation.
    """

    def __init__(self, client):
        self._client = client
        self._convos = {}

    def convo_for(self, did: str) -> str:
        if did not in self._convos:
            reply = self._client.convo_for_members([did])
            self._convos[did] = reply["convo"]["id"]
        return self._convos[did]

    def __call__(self, did: str, text: str) -> None:
        self._client.send_message(self.convo_for(did), text)


class AllowlistSender:
    """Refuses to message anyone outside an explicit list.

    Build step 4 is "chat send, to one DID only". That constraint deserves to
    be enforced by the code rather than by remembering, because the failure
    mode is messaging real strangers from a half-finished bot.

    Blocked sends raise rather than passing silently: a test run that quietly
    delivered nothing would look identical to a successful one.
    """

    def __init__(self, inner, allowed):
        self._inner = inner
        self._allowed = set(allowed)

    def __call__(self, did: str, text: str) -> None:
        if did not in self._allowed:
            raise PermissionError(
                "refusing to message %s: not in the send allowlist" % did
            )
        self._inner(did, text)
