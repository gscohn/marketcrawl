"""Publishing the daily public post to the bot's Bluesky profile.

ELI5: a DM goes through Bluesky's private chat service; a public post is
different. It's a record the bot writes into its own account, the same thing
the app does when you tap "Post". This file builds that record and sends it.

Two parts, kept apart so the first can be tested without a network:
  - post_record(): the post itself, with "facets" (Bluesky's word for the
    marked-up bits of text). Bluesky only makes a $NVDA cashtag clickable
    when the post says exactly which bytes of the text it covers.
  - PostClient / BlueskyPoster: logs in and publishes it.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .chat import USER_AGENT, ChatClient, link_facets

# The same rule Bluesky's own app uses to spot a cashtag (atproto's
# rich-text code): a $, then a letter, then up to four letters or digits,
# with a space, bracket or the start of the text before it and a space,
# punctuation or the end after it. BRK-B or SHOP.TO aren't cashtags, so
# they stay plain symbols.
CASHTAG = re.compile(r"(^|\s|\()(\$[A-Za-z][A-Za-z0-9]{0,4})(?=\s|$|[.,;:!?)\"'’])")
_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,4}$")


def cashtag(symbol: str) -> str:
    """'NVDA' -> '$NVDA'; symbols Bluesky can't tag are left as they are."""
    return "$" + symbol if _TICKER.match(symbol) else symbol


def cashtag_facets(text: str) -> list:
    """Mark each $TICKER as a tag. Offsets count bytes, not characters
    (an emoji is 4 bytes), which is what Bluesky expects."""
    facets = []
    for match in CASHTAG.finditer(text):
        tag = match.group(2)
        start = len(text[: match.start(2)].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": start + len(tag.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag.upper()}],
        })
    return facets


def post_record(text: str, now: datetime) -> dict:
    """The post, ready to publish."""
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "langs": ["en"],
    }
    facets = cashtag_facets(text) + link_facets(text)
    if facets:
        record["facets"] = facets
    return record


class PostClient(ChatClient):
    """The original Bluesky client, reused for its login and token refresh.
    One difference: its requests are addressed to the chat service, and a
    post must go to the bot's own account instead, so that address is left
    off."""

    def _headers(self) -> dict:
        headers = super()._headers()
        headers.pop("atproto-proxy", None)
        headers["User-Agent"] = USER_AGENT
        return headers

    def create_post(self, record: dict) -> dict:
        if self.did is None:
            self.login()
        return self._call("POST", "com.atproto.repo.createRecord", payload={
            "repo": self.did, "collection": "app.bsky.feed.post", "record": record,
        })


class BlueskyPoster:
    """What the scheduler calls with the day's post text (see FeedRouter)."""

    def __init__(self, client: PostClient, clock=None):
        self._client = client
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, text: str) -> None:
        reply = self._client.create_post(post_record(text, self._clock()))
        print("--- public post published: %s\n%s" % (reply.get("uri"), text), flush=True)
