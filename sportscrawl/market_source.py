"""The Yahoo adapter: fetches closing prices for the scheduler.

ELI5: when it's time to send messages, the scheduler says "get me today's
closing prices for these symbols". This file downloads each symbol once,
remembers the answer for a few minutes (so 100 people following AAPL cost one
download, not 100), and reports which symbols it couldn't get.

It deliberately has the same shape as the original EspnSource
(`fetch_window(leagues, now)` returning a WindowResult, plus a `health` dict),
so the scheduler can't tell which one it's talking to. That's the "swap the
source without touching the scheduler" design the original author built in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .http import FetchError, Fetcher
from .market_schedule import filing_window
from .nyse_calendar import new_york_date
from .source import LeagueHealth, WindowResult
from .yahoo import (ParseError, chart_url, extremes, full_history_url, latest_quote,
                    mark_milestones, parse_chart, quote_for, symbol_of)

# How long a download stays fresh. Short, because after the close the
# scheduler retries every minute while a symbol is late, and each retry
# should see new data.
CHART_TTL = timedelta(minutes=2)
# The full history (for record highs and 52-week lows) is big and only
# matters up to yesterday, so it's downloaded about once a day.
LONG_TTL = timedelta(hours=12)

# Yahoo refuses known library names (python-requests, curl) with a 429 and
# serves a plain product name. So we say who we are, honestly.
USER_AGENT = "marketcrawl/0.1 (+https://github.com/gscohn/marketcrawl)"


@dataclass
class _CacheEntry:
    fetched_at: datetime
    history: object


class YahooSource:
    def __init__(self, fetcher=None, ttl=CHART_TTL, range_="1mo", long_ttl=LONG_TTL):
        self._fetcher = fetcher or Fetcher(user_agent=USER_AGENT)
        self._ttl = ttl
        # How much history to ask for. A month covers yesterday's close and
        # "normal" volume; replay asks for more so it can walk back in time.
        self._range = range_
        self._charts = {}
        self._long_ttl = long_ttl
        self._long = {}
        self.health = {}

    def history(self, symbol: str, now: datetime):
        """One symbol's recent closes, cached. Raises FetchError/ParseError."""
        cached = self._charts.get(symbol)
        if cached is not None and now - cached.fetched_at < self._ttl:
            return cached.history

        payload = self._fetcher.get_json(chart_url(symbol, self._range))
        history = parse_chart(payload, symbol)
        self._charts[symbol] = _CacheEntry(fetched_at=now, history=history)
        return history

    def milestones(self, quote, now: datetime):
        """Mark a quote as a record high or 52-week low if it is one.

        Nice to have, never essential: if the long history can't be had,
        the quote goes out unmarked rather than late.
        """
        cached = self._long.get(quote.symbol)
        if cached is None or now - cached.fetched_at >= self._long_ttl:
            try:
                payload = self._fetcher.get_json(full_history_url(quote.symbol))
                history = parse_chart(payload, quote.symbol)
            except (FetchError, ParseError):
                # Remember the failure too (as None), so a missing history
                # isn't re-downloaded every minute.
                history = None
            cached = _CacheEntry(fetched_at=now, history=history)
            self._long[quote.symbol] = cached
        if cached.history is None:
            return quote
        return mark_milestones(quote, extremes(cached.history, quote.close_date))

    def fetch_window(self, leagues, now: datetime) -> WindowResult:
        """Closing prices for today's New York session, for every symbol.

        A symbol "fails" if the download broke OR if its close isn't final
        yet. Both mean the same thing to the scheduler: "not ready, try again
        next tick", and after a grace period, "say some prices are missing".
        """
        day = new_york_date(now)
        result = WindowResult()

        for league in leagues:
            health = self.health.setdefault(league, LeagueHealth(league=league))
            health.last_fetch_at = now
            try:
                quote = quote_for(self.history(symbol_of(league), now), day)
            except (FetchError, ParseError):
                quote = None

            if quote is None:
                health.consecutive_failures += 1
                result.failed_leagues.add(league)
                continue

            health.last_success_at = now
            health.consecutive_failures = 0
            health.last_event_date = quote.close_date
            result.events.append(self.milestones(quote, now))

        return result

    def latest(self, symbols, now: datetime) -> list:
        """Each symbol's most recent finished close, for a newcomer's first
        report. Symbols we can't get are simply left out."""
        quotes = []
        for symbol in symbols:
            try:
                quote = latest_quote(self.history(symbol, now))
            except (FetchError, ParseError):
                continue
            if quote is not None:
                quotes.append(self.milestones(quote, now))
        return quotes

    def lookup(self, symbol: str, now: datetime):
        """Does this symbol exist? For the `add` command.

        Returns the History, None for "Yahoo has never heard of it" (a 404),
        and raises FetchError for "Yahoo didn't answer", so the reply can tell
        those two apart instead of blaming the user for an outage.
        """
        try:
            return self.history(symbol, now)
        except FetchError as exc:
            # The Fetcher doesn't hand back the status code, only a message.
            # A 404 is the one answer that means "no such symbol".
            if "http 404" in str(exc):
                return None
            raise
        except ParseError:
            return None


class MarketSource:
    """Prices from Yahoo, plus (optionally) filings from the SEC.

    ELI5: the scheduler only knows how to talk to one source. This one
    asks Yahoo for prices and EDGAR for filings, and hands both back in the
    same pile, the way a waiter brings food from two kitchens on one tray.
    The formatter sorts the pile out again.

    Only prices count toward "this symbol failed". A missing filing list
    never delays or spoils anyone's message.
    """

    def __init__(self, prices, filings=None):
        self._prices = prices
        self._filings = filings

    @property
    def health(self):
        return self._prices.health

    def lookup(self, symbol: str, now: datetime):
        return self._prices.lookup(symbol, now)

    def latest(self, symbols, now: datetime) -> list:
        return self._prices.latest(symbols, now)

    def milestones(self, quote, now: datetime):
        """Mark a quote as a record high or 52-week low if it is one.

        Nice to have, never essential: if the long history can't be had,
        the quote goes out unmarked rather than late.
        """
        cached = self._long.get(quote.symbol)
        if cached is None or now - cached.fetched_at >= self._long_ttl:
            try:
                payload = self._fetcher.get_json(full_history_url(quote.symbol))
                history = parse_chart(payload, quote.symbol)
            except (FetchError, ParseError):
                # Remember the failure too (as None), so a missing history
                # isn't re-downloaded every minute.
                history = None
            cached = _CacheEntry(fetched_at=now, history=history)
            self._long[quote.symbol] = cached
        if cached.history is None:
            return quote
        return mark_milestones(quote, extremes(cached.history, quote.close_date))

    def fetch_window(self, leagues, now: datetime) -> WindowResult:
        leagues = list(leagues)
        result = self._prices.fetch_window(leagues, now)
        window = filing_window(new_york_date(now)) if self._filings else None
        if window is not None:
            start, end = window
            symbols = [symbol_of(league) for league in leagues]
            result.events.extend(self._filings.filings(symbols, start, end, now))
        return result
