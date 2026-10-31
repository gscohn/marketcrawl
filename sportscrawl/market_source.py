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
from .nyse_calendar import new_york_date
from .source import LeagueHealth, WindowResult
from .yahoo import ParseError, chart_url, parse_chart, quote_for, symbol_of

# How long a download stays fresh. Short, because after the close the
# scheduler retries every minute while a symbol is late, and each retry
# should see new data.
CHART_TTL = timedelta(minutes=2)

# Yahoo refuses known library names (python-requests, curl) with a 429 and
# serves a plain product name. So we say who we are, honestly.
USER_AGENT = "marketcrawl/0.1 (+https://github.com/gscohn/marketcrawl)"


@dataclass
class _CacheEntry:
    fetched_at: datetime
    history: object


class YahooSource:
    def __init__(self, fetcher=None, ttl=CHART_TTL, range_="5d"):
        self._fetcher = fetcher or Fetcher(user_agent=USER_AGENT)
        self._ttl = ttl
        # How much history to ask for. Five days covers a normal morning;
        # replay asks for a month so it can walk backwards in time.
        self._range = range_
        self._charts = {}
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
            result.events.append(quote)

        return result

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
