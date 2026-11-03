"""Publishing the daily public post to the bot's Bluesky profile.

ELI5: a DM goes through Bluesky's private chat service; a public post is
different. It's a record the bot writes into its own account, the same thing
the app does when you tap "Post". This file builds that record and sends it.

Two parts, kept apart so the first can be tested without a network:
  - post_record(): the post itself, with "facets" (Bluesky's word for the
    marked-up bits of text). Bluesky only makes a $NVDA cashtag clickable
    when the post says exactly which bytes of the text it covers. Long web
    addresses are shortened on screen the way the Bluesky app does it
    ("sec.gov/Archives/edg..."), with the full address kept in the link.
  - PostClient / BlueskyPoster: logs in and publishes it. Text holding
    POST_BREAK is several posts, published as a thread (each one a reply to
    the one before), for days with more to say than one post holds.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from urllib.parse import urlsplit

from .chat import URL_PATTERN, USER_AGENT, ChatClient

# The same rule Bluesky's own app uses to spot a cashtag (atproto's
# rich-text code): a $, then a letter, then up to four letters or digits,
# with a space, bracket or the start of the text before it and a space,
# punctuation or the end after it. BRK-B or SHOP.TO aren't cashtags, so
# they stay plain symbols.
CASHTAG = re.compile(r"(^|\s|\()(\$[A-Za-z][A-Za-z0-9]{0,4})(?=\s|$|[.,;:!?)\"'’])")
_TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,4}$")

# Where one post ends and the next in the thread begins. An ASCII "record
# separator" character: never in real text, so it can't split by accident.
POST_BREAK = "\n\x1e\n"


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


def short_url(url: str) -> str:
    """What a link looks like on screen, the way Bluesky's own app shows a
    pasted one: the site plus the start of the path, then "...".
    "https://www.sec.gov/Archives/edgar/data/320193/..." -> "sec.gov/Archives/edg...".
    (The app keeps "www."; dropping it saves four characters a line.)"""
    parts = urlsplit(url)
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    path = "" if parts.path == "/" else parts.path
    path += ("?" + parts.query if parts.query else "") + ("#" + parts.fragment if parts.fragment else "")
    return host + (path[:13] + "..." if len(path) > 15 else path)


def shorten_links(text: str):
    """Swap each web address for its short form. Returns the new text and
    the link facets that point each short form at the full address.
    Offsets count bytes, as in cashtag_facets."""
    out, facets, used = [], [], 0
    for match in URL_PATTERN.finditer(text):
        out.append(text[used: match.start()])
        start = len("".join(out).encode("utf-8"))
        shown = short_url(match.group(0))
        out.append(shown)
        facets.append({
            "index": {"byteStart": start, "byteEnd": start + len(shown.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": match.group(0)}],
        })
        used = match.end()
    out.append(text[used:])
    return "".join(out), facets


def shown_length(text: str) -> int:
    """How long a post will be once its links are shortened: the number
    that has to stay under Bluesky's limit."""
    return len(shorten_links(text)[0])


def post_record(text: str, now: datetime, reply: dict | None = None) -> dict:
    """The post, ready to publish. `reply` makes it part of a thread."""
    text, link_facets = shorten_links(text)
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "langs": ["en"],
    }
    facets = cashtag_facets(text) + link_facets
    if facets:
        record["facets"] = facets
    if reply:
        record["reply"] = reply
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
        self._unfinished = None  # (text, next part, thread so far)

    def __call__(self, text: str) -> None:
        parts = text.split(POST_BREAK)
        # If a thread broke halfway last time (the scheduler then retries
        # the whole text a minute later), carry on from where it stopped
        # instead of posting the first part twice.
        if self._unfinished and self._unfinished[0] == text:
            _, done, thread = self._unfinished
        else:
            done, thread = 0, None
        for number, part in enumerate(parts[done:], start=done):
            self._unfinished = (text, number, thread)
            published = self._client.create_post(post_record(part, self._clock(), thread))
            print("--- public post published: %s\n%s" % (published.get("uri"), part), flush=True)
            # The next part replies to this one. Bluesky wants both the
            # first post of the thread (root) and the one being answered
            # (parent), each by address (uri) and fingerprint (cid).
            here = {"uri": published.get("uri"), "cid": published.get("cid")}
            thread = {"root": thread["root"] if thread else here, "parent": here}
        self._unfinished = None
