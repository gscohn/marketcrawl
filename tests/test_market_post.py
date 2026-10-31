"""The public post record: cashtags and their byte offsets. No network."""

from __future__ import annotations

from datetime import datetime, timezone

from sportscrawl.market_post import BlueskyPoster, cashtag, cashtag_facets, post_record

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
