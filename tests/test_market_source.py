"""Yahoo adapter tests. A fake transport stands in for the network."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sportscrawl.http import FetchError, Fetcher, Response
from sportscrawl.market_source import USER_AGENT, YahooSource
from sportscrawl.yahoo import chart_url

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"

# 4:15pm New York time on Friday Oct 30, 2026: just after the close.
AFTER_CLOSE = datetime(2026, 10, 30, 20, 15, tzinfo=timezone.utc)

AAPL = "market/AAPL"
GSPC = "market/^GSPC"


def body(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeTransport:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []
        self.headers = None

    def __call__(self, url, timeout, headers):
        self.calls.append(url)
        self.headers = headers
        return self.replies.get(url, Response(404, body("not_found.json")))


@pytest.fixture
def transport():
    return FakeTransport({
        chart_url("AAPL"): Response(200, body("aapl_5d.json")),
        chart_url("^GSPC"): Response(200, body("gspc_5d.json")),
    })


def source_for(transport):
    return YahooSource(fetcher=Fetcher(transport=transport, sleep=lambda _: None,
                                       user_agent=USER_AGENT))


def test_fetches_todays_close_for_each_symbol(transport):
    result = source_for(transport).fetch_window([AAPL, GSPC], AFTER_CLOSE)
    assert {q.symbol for q in result.events} == {"AAPL", "^GSPC"}
    assert not result.failed_leagues


def test_says_who_it_is_honestly(transport):
    source_for(transport).fetch_window([AAPL], AFTER_CLOSE)
    assert transport.headers["User-Agent"].startswith("marketcrawl/")


def test_one_download_per_symbol_however_many_followers(transport):
    source = source_for(transport)
    source.fetch_window([AAPL], AFTER_CLOSE)
    source.fetch_window([AAPL], AFTER_CLOSE + timedelta(seconds=30))
    assert len(transport.calls) == 1


def test_cache_expires(transport):
    source = source_for(transport)
    source.fetch_window([AAPL], AFTER_CLOSE)
    source.fetch_window([AAPL], AFTER_CLOSE + timedelta(minutes=5))
    assert len(transport.calls) == 2


def test_a_broken_symbol_fails_alone(transport):
    result = source_for(transport).fetch_window([AAPL, "market/NOPE"], AFTER_CLOSE)
    assert [q.symbol for q in result.events] == ["AAPL"]
    assert result.failed_leagues == {"market/NOPE"}


def test_no_close_for_today_counts_as_failed(transport):
    # Asking on Monday, but the saved data stops on Friday: Monday's close
    # isn't there, so "not ready".
    monday = datetime(2026, 11, 2, 21, 15, tzinfo=timezone.utc)
    result = source_for(transport).fetch_window([AAPL], monday)
    assert result.failed_leagues == {AAPL}


def test_health_is_tracked_per_symbol(transport):
    source = source_for(transport)
    source.fetch_window([AAPL, "market/NOPE"], AFTER_CLOSE)
    assert source.health[AAPL].consecutive_failures == 0
    assert source.health[AAPL].last_event_date.isoformat() == "2026-10-30"
    assert source.health["market/NOPE"].consecutive_failures == 1


def test_lookup_finds_a_real_symbol(transport):
    assert source_for(transport).lookup("AAPL", AFTER_CLOSE).name == "Apple Inc."


def test_lookup_returns_none_for_an_unknown_symbol(transport):
    assert source_for(transport).lookup("NOPE", AFTER_CLOSE) is None


def test_lookup_raises_when_yahoo_is_down():
    down = FakeTransport({chart_url("AAPL"): Response(503, "")})
    with pytest.raises(FetchError):
        source_for(down).lookup("AAPL", AFTER_CLOSE)


# -- prices and filings together -------------------------------------------

from sportscrawl.edgar import Filing  # noqa: E402
from sportscrawl.market_source import MarketSource  # noqa: E402


class FakeFilings:
    def __init__(self):
        self.asked = None

    def filings(self, symbols, start, end, now):
        self.asked = (list(symbols), start, end)
        return [Filing(symbol="AAPL", form="10-K", accepted=end, url="https://x")]


def test_filings_ride_along_with_prices(transport):
    filings = FakeFilings()
    result = MarketSource(source_for(transport), filings).fetch_window([AAPL, GSPC], AFTER_CLOSE)
    assert {type(e).__name__ for e in result.events} == {"Quote", "Filing"}
    assert filings.asked[0] == ["AAPL", "^GSPC"]
    assert filings.asked[2] == AFTER_CLOSE  # today's window ends at 4:15pm


def test_without_filings_it_is_just_prices(transport):
    result = MarketSource(source_for(transport)).fetch_window([AAPL], AFTER_CLOSE)
    assert [type(e).__name__ for e in result.events] == ["Quote"]
