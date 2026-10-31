"""The public post: writing it, routing it, and the feed as a subscriber."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

from sportscrawl import market_feed
from sportscrawl.db import connect, migrate
from sportscrawl.market_feed import (CALL_TO_ACTION, FEED_DID, POST_LIMIT, FeedRouter,
                                     _dollars, format_feed_post, format_for, top_movers)
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
    for name in ("largest_market_cap.json", "day_gainers.json", "day_losers.json"):
        found += parse_screener(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
    return found


def test_screener_parses():
    gainers = parse_screener(json.loads((FIXTURES / "day_gainers.json").read_text()))
    assert gainers[0].symbol == "CDNA" and gainers[0].day == FRIDAY
    assert round(gainers[0].pct, 1) == 22.2


def test_value_change_is_measured_from_before_the_move():
    # Worth $75 after falling 25%: it was worth $100, so it lost $25.
    m = Mover(symbol="X", name="X", pct=-25.0, market_cap=75.0, day=FRIDAY)
    assert round(m.value_change, 6) == -25.0


def test_a_giant_moving_a_little_beats_a_minnow_moving_a_lot():
    giant = Mover(symbol="AMZN", name="Amazon", pct=2.0, market_cap=2.5e12, day=FRIDAY)
    minnow = Mover(symbol="TINY", name="Tiny", pct=500.0, market_cap=6e8, day=FRIDAY)
    assert top_movers([minnow, giant], FRIDAY, count=1) == [giant]


def test_each_company_once():
    a = Mover(symbol="GOOGL", name="Alphabet Inc.", pct=3.0, market_cap=3e12, day=FRIDAY)
    c = Mover(symbol="GOOG", name="Alphabet Inc.", pct=3.0, market_cap=3e12, day=FRIDAY)
    assert [m.symbol for m in top_movers([a, c, a], FRIDAY)] == ["GOOGL"]


def test_dollars():
    assert _dollars(-2.09e11) == "-$209B"
    assert _dollars(8.5e8) == "+$850M"
    assert _dollars(1.4e12) == "+$1.4T"


def test_the_whole_post():
    text = format_feed_post(INDEXES + movers(), FRIDAY).text
    assert text == (
        "Fri Oct 30 close\n"
        "\N{CHART WITH DOWNWARDS TREND} S&P 500 -1.82%  "
        "\N{COLLISION SYMBOL} Nasdaq -2.86%  "
        "\N{CHART WITH DOWNWARDS TREND} Dow -1.09%\n"
        "\n"
        "Biggest swings in company value\n"
        "\N{CHART WITH DOWNWARDS TREND} $NVDA -4.2% (-$218B)\n"
        "\N{COLLISION SYMBOL} $INTC -28.6% (-$140B)\n"
        "\N{COLLISION SYMBOL} $TSM -5.9% (-$133B)\n"
        "\n" + CALL_TO_ACTION
    )
    assert len(text) <= POST_LIMIT


def test_no_index_prices_means_no_post_not_an_apology():
    assert format_feed_post(movers(), FRIDAY).kind == "silent"


def test_movers_from_another_day_are_left_out():
    stale = [Mover(symbol="OLD", name="Old", pct=50.0, market_cap=1e12, day=date(2026, 10, 29))]
    text = format_feed_post(INDEXES + stale, FRIDAY).text
    assert "OLD" not in text and "Biggest swings" not in text


def test_feed_gets_the_post_people_get_their_dm():
    feed_subs = market_feed.feed_subs()
    assert "Biggest swings" in format_for(INDEXES + movers(), feed_subs, "UTC", NOW).text
    person = [Sub(kind="ticker", league="market/^GSPC")]
    dm = format_for(INDEXES + movers(), person, "UTC", NOW).text
    assert "Biggest swings" not in dm and "S&P 500" not in dm  # DM uses the index's own name
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
