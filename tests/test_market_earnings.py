"""The public earnings post: which reports, how it's written, when it's due."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sportscrawl.edgar import Filing, MajorEarnings
from sportscrawl.market_feed import (EARNINGS_DID, EARNINGS_LEAGUE, FEED_KIND, POST_LIMIT,
                                     FeedRouter, classify_for, format_earnings_post, format_for)
from sportscrawl.market_post import POST_BREAK, shown_length
from sportscrawl.market_schedule import earnings_due
from sportscrawl.market_source import MarketSource
from sportscrawl.models import Sub
from sportscrawl.nyse_calendar import NEW_YORK
from sportscrawl.scheduler import DUE, NOT_DUE
from sportscrawl.yahoo import Mover

MONDAY = date(2026, 11, 2)
# 5:15pm New York (EST from Nov 1): the earnings post's time.
EARNINGS_TIME = datetime(2026, 11, 2, 22, 15, tzinfo=timezone.utc)
SEC = "https://www.sec.gov/Archives/edgar/data/%d/0000000000/r.htm"


def report(symbol, name, cap, n=1):
    filing = Filing(symbol=symbol, form="8-K", url=SEC % n,
                    accepted=datetime(2026, 11, 2, 16, 5, tzinfo=NEW_YORK))
    return MajorEarnings(filing=filing, name=name, market_cap=cap)


EARNINGS_SUBS = [Sub(kind=FEED_KIND, league=EARNINGS_LEAGUE)]


def test_dm_format_with_cashtags_biggest_company_first():
    events = [report("MSFT", "Microsoft Corporation", 3e12, 1),
              report("AAPL", "Apple Inc.", 4e12, 2)]
    text = format_earnings_post(events, MONDAY).text
    assert text == (
        "Mon Nov 2 earnings\n"
        "$AAPL Apple earnings release (8-K) " + SEC % 2 + "\n"
        "$MSFT Microsoft earnings release (8-K) " + SEC % 1
    )


def test_nothing_to_report_means_no_post():
    assert format_earnings_post([], MONDAY).kind == "silent"


def test_a_busy_day_becomes_a_thread_each_part_within_the_limit():
    events = [report("C%d" % i, "Company %d" % i, 1e12 - i, i) for i in range(12)]
    parts = format_earnings_post(events, MONDAY).text.split(POST_BREAK)
    assert len(parts) > 1
    assert all(shown_length(p) <= POST_LIMIT for p in parts)
    assert sum(p.count("earnings release") for p in parts) == 12


def test_a_very_busy_day_stops_at_four_posts_and_counts_the_rest():
    events = [report("C%d" % i, "Company %d" % i, 1e12 - i, i) for i in range(60)]
    parts = format_earnings_post(events, MONDAY).text.split(POST_BREAK)
    assert len(parts) == 4
    shown = sum(p.count("earnings release") for p in parts)
    assert parts[-1].endswith("+%d more" % (60 - shown))
    assert all(shown_length(p) <= POST_LIMIT for p in parts)


def test_the_scheduler_gives_the_earnings_feed_its_own_post():
    events = [report("AAPL", "Apple Inc.", 4e12)]
    assert "earnings" in format_for(events, EARNINGS_SUBS, "UTC", EARNINGS_TIME).text
    # ...and nobody's DM picks the big-company reports up.
    person = [Sub(kind="ticker", league="market/AAPL")]
    assert format_for(events, person, "UTC", EARNINGS_TIME).kind == "silent"


def test_earnings_feed_is_due_75_minutes_after_the_close():
    row = {"did": EARNINGS_DID, "tz": "America/New_York", "last_evaluated_date": None}
    assert earnings_due(MONDAY) == EARNINGS_TIME
    assert classify_for(row, EARNINGS_TIME - timedelta(minutes=1))[0] == NOT_DUE
    assert classify_for(row, EARNINGS_TIME)[0] == DUE
    person = dict(row, did="did:plc:someone")
    assert classify_for(person, EARNINGS_TIME - timedelta(hours=1))[0] == DUE


def test_router_posts_the_earnings_feed():
    posted = []
    FeedRouter(lambda did, text: None, poster=posted.append)(EARNINGS_DID, "x")
    assert posted == ["x"]


# -- fetching ---------------------------------------------------------------

class FakePrices:
    health = {}

    def fetch_window(self, leagues, now):
        from sportscrawl.source import WindowResult
        return WindowResult()

    def movers(self, now):
        return [Mover(symbol="AAPL", name="Apple Inc.", pct=1, market_cap=4e12, day=MONDAY),
                Mover(symbol="TINY", name="Tiny", pct=50, market_cap=5e9, day=MONDAY)]


class FakeFilings:
    def __init__(self):
        self.asked = None

    def filings(self, symbols, start, end, now):
        self.asked = (list(symbols), start, end)
        accepted = end - timedelta(hours=1)
        return [Filing(symbol="AAPL", form="8-K", accepted=accepted, url="https://a"),
                Filing(symbol="AAPL", form="10-K", accepted=accepted, url="https://b")]


def test_big_companies_releases_are_fetched_once_the_post_is_due():
    filings = FakeFilings()
    source = MarketSource(FakePrices(), filings, movers=True)
    found = [e for e in source.fetch_window([], EARNINGS_TIME).events
             if isinstance(e, MajorEarnings)]
    assert [(e.filing.symbol, e.filing.form, e.name) for e in found] == [("AAPL", "8-K", "Apple Inc.")]
    assert filings.asked[0] == ["AAPL"]  # $5B Tiny isn't "major"
    assert filings.asked[1] == earnings_due(date(2026, 10, 30))  # since Friday's post


def test_not_fetched_at_dm_time():
    filings = FakeFilings()
    source = MarketSource(FakePrices(), filings, movers=True)
    events = source.fetch_window([], EARNINGS_TIME - timedelta(hours=1)).events
    assert not any(isinstance(e, MajorEarnings) for e in events)
    assert filings.asked[0] == []  # only the DMs' own lookup (nobody follows anything here)
