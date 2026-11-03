"""The public post record: cashtags and their byte offsets. No network."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sportscrawl.market_post import (POST_BREAK, BlueskyPoster, cashtag, cashtag_facets,
                                     post_record, short_url)

NOW = datetime(2026, 11, 2, 21, 20, tzinfo=timezone.utc)


def test_cashtag_only_for_symbols_bluesky_can_tag():
    assert cashtag("NVDA") == "$NVDA"
    assert cashtag("BRK-B") == "BRK-B"      # a dash isn't allowed in a cashtag
    assert cashtag("GOOGLE") == "GOOGLE"    # six letters is too long
    assert cashtag("^GSPC") == "^GSPC"


def test_facet_offsets_count_bytes_not_characters():
    text = "\N{CHART WITH DOWNWARDS TREND} $NVDA -4.2%"  # the emoji is 4 bytes
    [facet] = cashtag_facets(text)
    assert facet["index"] == {"byteStart": 5, "byteEnd": 10}
    assert text.encode("utf-8")[5:10] == b"$NVDA"
    assert facet["features"] == [{"$type": "app.bsky.richtext.facet#tag", "tag": "$NVDA"}]


def test_dollar_amounts_are_not_cashtags():
    assert cashtag_facets("$NVDA -4.2% (-$209B)\n$TSM (+$1.4T)") == [
        {"index": {"byteStart": 0, "byteEnd": 5},
         "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "$NVDA"}]},
        {"index": {"byteStart": 21, "byteEnd": 25},
         "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": "$TSM"}]},
    ]


def test_post_record():
    record = post_record("Fri Oct 30 close\n$NVDA -4.2%", NOW)
    assert record["$type"] == "app.bsky.feed.post"
    assert record["createdAt"] == "2026-11-02T21:20:00Z"
    assert record["langs"] == ["en"]
    assert len(record["facets"]) == 1


def test_no_facets_key_when_nothing_to_mark():
    assert "facets" not in post_record("S&P 500 -1.82%", NOW)


def test_poster_publishes_the_record():
    posted = []

    class FakeClient:
        def create_post(self, record):
            posted.append(record)
            return {"uri": "at://bot/app.bsky.feed.post/1"}

    BlueskyPoster(FakeClient(), clock=lambda: NOW)("$NVDA -4.2%")
    assert posted[0]["text"] == "$NVDA -4.2%"


# -- links and threads --------------------------------------------------------

SEC = "https://www.sec.gov/Archives/edgar/data/320193/000032019326000105/aapl-20260930.htm"


def test_long_links_are_shortened_like_the_bluesky_app_does():
    assert short_url(SEC) == "sec.gov/Archives/edg..."
    assert short_url("https://example.com/") == "example.com"
    assert short_url("https://example.com/short") == "example.com/short"


def test_a_shortened_link_still_points_at_the_whole_address():
    record = post_record("\N{ROCKET} $AAPL release " + SEC, NOW)
    assert record["text"] == "\N{ROCKET} $AAPL release sec.gov/Archives/edg..."
    link = [f for f in record["facets"] if f["features"][0]["$type"].endswith("#link")][0]
    start, end = link["index"]["byteStart"], link["index"]["byteEnd"]
    assert record["text"].encode("utf-8")[start:end] == b"sec.gov/Archives/edg..."
    assert link["features"][0]["uri"] == SEC


class ThreadClient:
    def __init__(self, fail_on=None):
        self.posted, self.fail_on = [], fail_on

    def create_post(self, record):
        if record["text"] == self.fail_on:
            self.fail_on = None  # fails once, then works
            raise RuntimeError("bluesky hiccup")
        self.posted.append(record)
        n = len(self.posted)
        return {"uri": "at://bot/post/%d" % n, "cid": "cid%d" % n}


def test_several_parts_become_a_thread():
    client = ThreadClient()
    BlueskyPoster(client, clock=lambda: NOW)(POST_BREAK.join(["one", "two", "three"]))
    first, second, third = client.posted
    assert "reply" not in first
    assert second["reply"] == {"root": {"uri": "at://bot/post/1", "cid": "cid1"},
                               "parent": {"uri": "at://bot/post/1", "cid": "cid1"}}
    assert third["reply"]["root"]["uri"] == "at://bot/post/1"
    assert third["reply"]["parent"]["uri"] == "at://bot/post/2"


def test_a_thread_that_broke_halfway_carries_on_instead_of_repeating():
    client = ThreadClient(fail_on="two")
    poster = BlueskyPoster(client, clock=lambda: NOW)
    text = POST_BREAK.join(["one", "two", "three"])
    with pytest.raises(RuntimeError):
        poster(text)
    poster(text)  # the scheduler's retry a minute later
    assert [r["text"] for r in client.posted] == ["one", "two", "three"]
    assert client.posted[1]["reply"]["parent"]["uri"] == "at://bot/post/1"
