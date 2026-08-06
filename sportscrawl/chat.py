"""Bluesky chat transport.

chat.bsky.* is centralised Bluesky infrastructure, not on-protocol DMs. The
atproto part of this project is identity and the inbound command channel;
delivery depends on Bluesky the company. Spec sections 8 and 17.

Chat endpoints are proxied through the PDS to did:web:api.bsky.chat, which is
what the atproto-proxy header does. Without it the calls 404.
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass

from .http import CONNECT_TIMEOUT, READ_TIMEOUT, USER_AGENT, FetchError

PLC_DIRECTORY = "https://plc.directory"

# Refresh this long before the token actually lapses. Measured 2026-08-06: an
# accessJwt from createSession lives exactly 2 hours. Refreshing early turns a
# guaranteed failure every two hours into an ordinary call.
TOKEN_REFRESH_MARGIN = 300
CHAT_PROXY = "did:web:api.bsky.chat#bsky_chat"


URL_PATTERN = re.compile(r"https?://[^\s<>\"]+[^\s<>\".,;:!?)\]]")


def link_facets(text: str) -> list:
    """Mark every URL in the message as a clickable link.

    Bluesky renders links from facets, not by parsing the text. Without them a
    URL is inert grey text the subscriber has to copy by hand, and the help
    message in section 9 exists to hand someone the config link.

    Auto-detection is not something to rely on: a bare `http://192.168.4.24:5000/...`
    is not linkified at all, and that is precisely the shape of a link with no
    TLD. Sending explicit facets removes the guesswork.

    Offsets are UTF-8 BYTE indices, not character indices. A digest containing
    "CF Montreal" with an accent would shift every subsequent link by one and
    silently produce a link over the wrong range.
    """
    encoded = text.encode("utf-8")
    facets = []
    for match in URL_PATTERN.finditer(text):
        uri = match.group(0)
        byte_start = len(text[: match.start()].encode("utf-8"))
        facets.append(
            {
                "index": {
                    "byteStart": byte_start,
                    "byteEnd": byte_start + len(uri.encode("utf-8")),
                },
                "features": [
                    {"$type": "app.bsky.richtext.facet#link", "uri": uri}
                ],
            }
        )
    return facets


@dataclass(frozen=True)
class Message:
    message_id: str
    convo_id: str
    sender_did: str
    text: str
    sent_at: str


def resolve_pds(did: str, transport) -> str:
    """Read the PDS endpoint out of the DID document.

    Not hardcoded to bsky.social: accounts get migrated between hosts, and the
    DID document is the only authority on where this one lives.
    """
    doc = transport("GET", "%s/%s" % (PLC_DIRECTORY, did), None, None)
    for service in doc.get("service", []):
        if service.get("type") == "AtprotoPersonalDataServer":
            return service["serviceEndpoint"].rstrip("/")
    raise FetchError("no PDS in DID document for %s" % did)


# Bluesky signals an expired session with HTTP 400 and an error code in the
# body, not 401. Matching only on 401 meant the re-login path never ran: the
# scheduler logged in at boot, the token expired about sixteen hours later, and
# every send after that failed identically and forever while the loop carried
# on ticking healthily. Twice.
AUTH_ERROR_CODES = frozenset(
    {"ExpiredToken", "InvalidToken", "AuthMissing", "AuthenticationRequired"}
)


def _is_auth_error(reply) -> bool:
    if reply.status_code == 401:
        return True
    if reply.status_code != 400:
        return False
    try:
        return reply.json().get("error") in AUTH_ERROR_CODES
    except ValueError:
        return False


def requests_transport(method, url, headers, payload, params=None):
    import requests

    reply = requests.request(
        method,
        url,
        headers=headers,
        json=payload,
        params=params,
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
    )
    if _is_auth_error(reply):
        raise Unauthorized(reply.text[:200])
    if reply.status_code != 200:
        raise FetchError("http %d from %s: %s" % (reply.status_code, url, reply.text[:200]))
    return reply.json()


class Unauthorized(Exception):
    """The access token was rejected. Recoverable by logging in again."""


def token_expiry(jwt: str):
    """Read the `exp` claim. Returns None if it cannot be parsed.

    Not verifying the signature, just reading a public claim to know when to
    log in again. Unparseable means fall back to the reactive path.
    """
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))["exp"]
    except Exception:
        return None


class ChatClient:
    def __init__(self, identifier: str, app_password: str, transport=None, pds=None):
        self._identifier = identifier
        self._password = app_password
        self._transport = transport or requests_transport
        self._pds = pds
        self._access = None
        self._expires_at = None
        self.did = None

    # -- session ----------------------------------------------------------

    @property
    def pds(self) -> str:
        if self._pds is None:
            self._pds = resolve_pds(self._identifier, self._plain)
        return self._pds

    def _plain(self, method, url, headers, payload, params=None):
        return self._transport(method, url, headers or {"User-Agent": USER_AGENT}, payload, params)

    def login(self) -> str:
        session = self._transport(
            "POST",
            "%s/xrpc/com.atproto.server.createSession" % self.pds,
            {"User-Agent": USER_AGENT},
            {"identifier": self._identifier, "password": self._password},
        )
        self._access = session["accessJwt"]
        self._expires_at = token_expiry(self._access)
        self.did = session["did"]
        return self.did

    def _expired(self) -> bool:
        if self._expires_at is None:
            return False
        return time.time() >= self._expires_at - TOKEN_REFRESH_MARGIN

    def _headers(self) -> dict:
        if self._access is None or self._expired():
            # Proactive. The reactive path below still exists, because a token
            # can be revoked or invalidated before its stated expiry.
            self.login()
        return {
            "User-Agent": USER_AGENT,
            "Authorization": "Bearer %s" % self._access,
            "atproto-proxy": CHAT_PROXY,
        }

    def _call(self, method, nsid, payload=None, params=None):
        url = "%s/xrpc/%s" % (self.pds, nsid)
        try:
            return self._transport(method, url, self._headers(), payload, params)
        except Unauthorized:
            # Access tokens expire. One retry with a fresh session, then give
            # up: a second 401 means the app password itself is bad, and
            # hammering a rejected credential is how accounts get locked.
            self._access = None
            self.login()
            return self._transport(method, url, self._headers(), payload, params)

    # -- conversations ----------------------------------------------------

    def list_convos(self, status=None, cursor=None, limit=50) -> dict:
        params = {"limit": limit}
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        return self._call("GET", "chat.bsky.convo.listConvos", params=params)

    def get_messages(self, convo_id: str, cursor=None, limit=50) -> dict:
        params = {"convoId": convo_id, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._call("GET", "chat.bsky.convo.getMessages", params=params)

    def send_message(self, convo_id: str, text: str) -> dict:
        message = {"text": text}
        facets = link_facets(text)
        if facets:
            message["facets"] = facets
        return self._call(
            "POST",
            "chat.bsky.convo.sendMessage",
            payload={"convoId": convo_id, "message": message},
        )

    def update_read(self, convo_id: str, message_id=None) -> dict:
        payload = {"convoId": convo_id}
        if message_id:
            payload["messageId"] = message_id
        return self._call("POST", "chat.bsky.convo.updateRead", payload=payload)

    def convo_for_members(self, dids) -> dict:
        return self._call(
            "GET", "chat.bsky.convo.getConvoForMembers", params={"members": list(dids)}
        )


def parse_messages(payload: dict, convo_id: str) -> list:
    """Pull the fields the command loop needs out of a getMessages response.

    Deleted messages come back without text; they are skipped rather than
    treated as an empty command.
    """
    messages = []
    for raw in payload.get("messages", []):
        if "text" not in raw:
            continue
        sender = (raw.get("sender") or {}).get("did")
        if not sender:
            continue
        messages.append(
            Message(
                message_id=str(raw["id"]),
                convo_id=convo_id,
                sender_did=sender,
                text=raw["text"],
                sent_at=raw.get("sentAt", ""),
            )
        )
    return messages
