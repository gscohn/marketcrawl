"""Chat transport tests. Hermetic: no test touches Bluesky."""

from __future__ import annotations

import pytest

from sportscrawl.chat import (
    CHAT_PROXY,
    link_facets,
    ChatClient,
    Unauthorized,
    parse_messages,
    resolve_pds,
)
from sportscrawl.http import FetchError

DID = "did:plc:kmx5rkipob7xh6lhgxzxxkxq"
PDS = "https://chalciporus.us-west.host.bsky.network"


class FakeTransport:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, method, url, headers, payload, params=None):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "payload": payload, "params": params})
        # Match on a suffix of the URL: the last path segment is the full nsid
        # ("com.atproto.server.createSession"), not the bare method name.
        key = next((k for k in self.responses if url.endswith(k)), None)
        reply = self.responses.get(key, {})
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else {}
        if isinstance(reply, Exception):
            raise reply
        return reply


def client(transport):
    return ChatClient(DID, "app-pass", transport=transport, pds=PDS)


SESSION = {"accessJwt": "token-1", "did": DID, "handle": "sportscrawl.app"}


def test_pds_is_read_from_the_did_document():
    """Not hardcoded to bsky.social: accounts get migrated between hosts."""
    transport = FakeTransport({DID: {"service": [
        {"type": "AtprotoPersonalDataServer", "serviceEndpoint": PDS + "/"}
    ]}})
    assert resolve_pds(DID, transport) == PDS


def test_a_did_document_without_a_pds_is_an_error():
    transport = FakeTransport({DID: {"service": []}})
    with pytest.raises(FetchError):
        resolve_pds(DID, transport)


def test_chat_calls_carry_the_proxy_header():
    """Without it the chat endpoints 404."""
    transport = FakeTransport({"createSession": SESSION, "listConvos": {"convos": []}})
    client(transport).list_convos()

    chat_call = transport.calls[-1]
    assert chat_call["headers"]["atproto-proxy"] == CHAT_PROXY
    assert chat_call["headers"]["Authorization"] == "Bearer token-1"


def test_login_happens_lazily_once():
    transport = FakeTransport({"createSession": SESSION, "listConvos": {"convos": []}})
    engine = client(transport)
    engine.list_convos()
    engine.list_convos()

    logins = [c for c in transport.calls if c["url"].endswith("createSession")]
    assert len(logins) == 1


def test_an_expired_token_is_retried_once_with_a_fresh_session():
    transport = FakeTransport({
        "createSession": [SESSION, {"accessJwt": "token-2", "did": DID}],
        "listConvos": [Unauthorized("expired"), {"convos": [{"id": "c1"}]}],
    })
    result = client(transport).list_convos()

    assert result["convos"] == [{"id": "c1"}]
    logins = [c for c in transport.calls if c["url"].endswith("createSession")]
    assert len(logins) == 2
    assert transport.calls[-1]["headers"]["Authorization"] == "Bearer token-2"


def test_a_second_rejection_is_not_retried_again():
    """A second 401 means the app password itself is bad, and hammering a
    rejected credential is how accounts get locked."""
    transport = FakeTransport({
        "createSession": [SESSION, SESSION],
        "listConvos": [Unauthorized("no"), Unauthorized("still no")],
    })
    with pytest.raises(Unauthorized):
        client(transport).list_convos()


def test_status_filter_is_passed_through():
    transport = FakeTransport({"createSession": SESSION, "listConvos": {"convos": []}})
    client(transport).list_convos(status="request")
    assert transport.calls[-1]["params"]["status"] == "request"


def test_send_message_shape():
    transport = FakeTransport({"createSession": SESSION, "sendMessage": {"id": "m1"}})
    client(transport).send_message("c1", "line one\nline two")

    payload = transport.calls[-1]["payload"]
    assert payload == {"convoId": "c1", "message": {"text": "line one\nline two"}}


def test_parse_messages_extracts_sender_and_text():
    payload = {"messages": [
        {"id": "m1", "text": "config", "sender": {"did": "did:plc:alice"},
         "sentAt": "2026-08-04T14:00:00Z"},
    ]}
    messages = parse_messages(payload, "c1")
    assert len(messages) == 1
    assert messages[0].sender_did == "did:plc:alice"
    assert messages[0].text == "config"
    assert messages[0].convo_id == "c1"


def test_deleted_messages_are_skipped_not_treated_as_empty_commands():
    payload = {"messages": [
        {"id": "m1", "sender": {"did": "did:plc:alice"}},          # deleted
        {"id": "m2", "text": "stop", "sender": {"did": "did:plc:alice"}},
    ]}
    messages = parse_messages(payload, "c1")
    assert [m.message_id for m in messages] == ["m2"]


# --------------------------------------------------------------------------
# Link facets
# --------------------------------------------------------------------------


def test_a_url_gets_a_link_facet():
    """Bluesky renders links from facets, not by parsing text. Without one the
    config link in the help message is inert and onboarding dead-ends."""
    facets = link_facets("Settings: https://sportscrawl.app/c/abc")
    assert len(facets) == 1
    assert facets[0]["features"][0]["uri"] == "https://sportscrawl.app/c/abc"
    assert facets[0]["features"][0]["$type"] == "app.bsky.richtext.facet#link"


def test_facet_offsets_are_utf8_bytes_not_characters():
    """An accented team name ahead of the link shifts every later offset."""
    text = "CF Montréal won. See https://sportscrawl.app/c/xyz"
    facet = link_facets(text)[0]
    encoded = text.encode("utf-8")
    covered = encoded[facet["index"]["byteStart"]:facet["index"]["byteEnd"]]
    assert covered.decode("utf-8") == "https://sportscrawl.app/c/xyz"


def test_a_url_without_a_tld_still_gets_a_facet():
    """A bare host:port is exactly what client auto-detection misses."""
    facets = link_facets("Open http://192.168.4.24:5000/c/abc on your phone")
    assert facets[0]["features"][0]["uri"] == "http://192.168.4.24:5000/c/abc"


def test_trailing_punctuation_is_not_part_of_the_link():
    facets = link_facets("See https://sportscrawl.app/privacy.")
    assert facets[0]["features"][0]["uri"] == "https://sportscrawl.app/privacy"


def test_a_message_without_urls_sends_no_facets():
    transport = FakeTransport({"createSession": SESSION, "sendMessage": {"id": "m1"}})
    client(transport).send_message("c1", "Tue Aug 4\n\nFinal\nDodgers 6, Padres 3")
    assert "facets" not in transport.calls[-1]["payload"]["message"]


def test_send_message_attaches_facets_automatically():
    transport = FakeTransport({"createSession": SESSION, "sendMessage": {"id": "m1"}})
    client(transport).send_message("c1", "Pick teams: https://sportscrawl.app/c/k9")
    message = transport.calls[-1]["payload"]["message"]
    assert message["facets"][0]["features"][0]["uri"] == "https://sportscrawl.app/c/k9"


# --------------------------------------------------------------------------
# Session expiry
# --------------------------------------------------------------------------


class FakeReply:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_an_expired_token_is_recognised_despite_being_a_400():
    """Bluesky returns 400 with an error code, not 401. Matching only on 401
    meant the re-login path never ran: the scheduler logged in at boot and
    every send after the token expired failed identically, forever."""
    from sportscrawl.chat import _is_auth_error

    assert _is_auth_error(FakeReply(400, {"error": "ExpiredToken",
                                          "message": "Token has expired"}))


@pytest.mark.parametrize("code", ["InvalidToken", "AuthMissing",
                                  "AuthenticationRequired"])
def test_the_other_auth_error_codes_are_recognised(code):
    from sportscrawl.chat import _is_auth_error

    assert _is_auth_error(FakeReply(400, {"error": code}))


def test_a_401_is_still_an_auth_error():
    from sportscrawl.chat import _is_auth_error

    assert _is_auth_error(FakeReply(401, None, "unauthorized"))


def test_an_ordinary_400_is_not_an_auth_error():
    """A malformed request must not trigger an endless login loop."""
    from sportscrawl.chat import _is_auth_error

    assert not _is_auth_error(FakeReply(400, {"error": "InvalidRequest",
                                              "message": "grapheme too big"}))


def test_a_400_with_no_json_body_is_not_an_auth_error():
    from sportscrawl.chat import _is_auth_error

    assert not _is_auth_error(FakeReply(400, None, "<html>gateway</html>"))


def test_an_expired_session_recovers_by_logging_in_again():
    """End to end through the client: the first call fails as expired, the
    client re-logins and the retry succeeds."""
    transport = FakeTransport({
        "createSession": [SESSION, {"accessJwt": "token-2", "did": DID}],
        "getConvoForMembers": [Unauthorized('{"error":"ExpiredToken"}'),
                               {"convo": {"id": "c1"}}],
    })
    result = client(transport).convo_for_members([DID])

    assert result["convo"]["id"] == "c1"
    assert transport.calls[-1]["headers"]["Authorization"] == "Bearer token-2"
