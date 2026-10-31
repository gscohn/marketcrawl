"""The public post: writing it, routing it, and the feed as a subscriber."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from sportscrawl import market_feed
from sportscrawl.db import connect, migrate
from sportscrawl.market_feed import (CALL_TO_ACTION, FEED_DID, POST_LIMIT, FeedRouter,
                                     format_feed_post, format_for)
from sportscrawl.market_repo import ticker_leagues
from sportscrawl.models import Sub
from sportscrawl.yahoo import Mover, Quote, parse_screener

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
FRIDAY = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, 20, 20, tzinfo=timezone.utc)


def index(symbol, close, prev):
    return Quote(symbol=symbol, name=symbol, is_index=True, close=close,
                 prev_close=prev, close_date=FRIDAY)


INDEXES = [index("^GSPC", 7580.07, 7720.39), index("^IXIC", 25437.0, 26185.9),
           index("^DJI", 51770.31, 52340.0)]


def movers():
    found = []
    for name in ("day_gainers.json", "day_losers.json"):
        found += parse_screener(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
    return found


def test_screener_parses():
    gainers = parse_screener(json.loads((FIXTURES / "day_gainers.json").read_text()))
    assert gainers[0].symbol == "CDNA" and gainers[0].day == FRIDAY
    assert round(gainers[0].pct, 1) == 22.2


def test_the_whole_post():
    text = format_feed_post(INDEXES + movers(), FRIDAY).text
    assert text == (
        "Fri Oct 30 close\n"
        "\n"
        "\N{CHART WITH DOWNWARDS TREND} S&P 500 7,580 -1.82%\n"
        "\N{COLLISION SYMBOL} Nasdaq 25,437 -2.86%\n"
        "\N{CHART WITH DOWNWARDS TREND} Dow 51,770 -1.09%\n"
        "\n"
        "Biggest movers\n"
        "\N{ROCKET} CDNA +22.2%  CPRI +16.3%  FDXF +14.9%\n"
        "\N{COLLISION SYMBOL} RDDT -29.2%  INTC -28.6%  CACC -19.3%\n"
        "\n" + CALL_TO_ACTION
    )
    assert len(text) <= POST_LIMIT


def test_no_index_prices_means_no_post_not_an_apology():
    assert format_feed_post(movers(), FRIDAY).kind == "silent"


def test_movers_from_another_day_are_left_out():
    stale = [Mover(symbol="OLD", name="Old", pct=50.0, day=date(2026, 10, 29))]
    text = format_feed_post(INDEXES + stale, FRIDAY).text
    assert "OLD" not in text and "Biggest movers" not in text


def test_long_names_trim_movers_to_fit():
    huge = [Mover(symbol="X" * 60 + str(i), name="x", pct=10.0 + i, day=FRIDAY)
            for i in range(6)]
    text = format_feed_post(INDEXES + huge, FRIDAY).text
    assert len(text) <= POST_LIMIT
    assert text.endswith(CALL_TO_ACTION)


def test_feed_gets_the_post_people_get_their_dm():
    feed_subs = market_feed.feed_subs()
    assert "Biggest movers" in format_for(INDEXES + movers(), feed_subs, "UTC", NOW).text
    person = [Sub(kind="ticker", league="market/^GSPC")]
    dm = format_for(INDEXES + movers(), person, "UTC", NOW).text
    assert "Biggest movers" not in dm and "S&P 500" not in dm  # DM uses the index's own name
    assert "^GSPC 7,580.07" in dm


def test_router_posts_the_feed_and_dms_everyone_else():
    posted, sent = [], []
    router = FeedRouter(lambda did, text: sent.append(did), poster=posted.append)
    router(FEED_DID, "public")
    router("did:plc:someone", "private")
    assert posted == ["public"] and sent == ["did:plc:someone"]


def test_enable_and_disable(tmp_path):
    db = connect(str(tmp_path / "feed.db"))
    migrate(db)
    market_feed.enable(db, NOW, "pepper")
    market_feed.enable(db, NOW, "pepper")  # twice is harmless
    assert ticker_leagues(db) == ["market/^DJI", "market/^GSPC", "market/^IXIC"]
    market_feed.disable(db, NOW, "pepper")
    assert ticker_leagues(db) == []
