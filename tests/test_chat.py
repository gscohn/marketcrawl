"""Chat transport tests. Hermetic: no test touches Bluesky."""

from __future__ import annotations

import pytest

from sportscrawl.chat import (
    CHAT_PROXY,
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
        nsid = url.rsplit("/", 1)[-1]
        reply = self.responses.get(nsid, {})
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
