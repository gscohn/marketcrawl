"""Reading Yahoo Finance's price charts.

ELI5: Yahoo has a web address that, for any stock symbol, hands back a big
blob of JSON: the company's name, and a list of "on this day it closed at this
price". This file turns that blob into a tidy Python object. It never touches
the network; downloading is somebody else's job (market_source.py). That split
is the same one the original project uses for ESPN (espn.py vs source.py),
and it is what lets the tests feed in saved JSON instead of calling Yahoo.

Like ESPN's, this API is undocumented. Nobody promised it will keep working,
which is why the parse is strict: anything we don't recognise is an error,
never a guess.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CHART_API = "https://query1.finance.yahoo.com/v8/finance/chart"


class ParseError(Exception):
    """The JSON wasn't a price chart we understand."""


@dataclass(frozen=True)
class Bar:
    """One day of trading, boiled down to the two numbers we use."""

    day: date  # the New York trading date
    close: float
    volume: int | None = None  # how many shares changed hands; None if Yahoo left it out


@dataclass(frozen=True)
class History:
    """Everything one chart download told us about one symbol.

    `market_time` is when Yahoo last saw a trade. `session_day` and
    `session_end` describe the trading session Yahoo calls "current": today's
    on a weekday, the next one on a weekend. If a bar belongs to that session
    and the last trade is before its end, that bar is still moving, like a
    game still being played.
    """

    symbol: str
    name: str
    is_index: bool
    bars: tuple
    market_time: datetime
    session_day: date | None
    session_end: datetime | None
    # The latest trade price. During market hours this is the live price;
    # after the close it's the closing price.
    price: float | None = None


@dataclass(frozen=True)
class Quote:
    """One symbol's closing price for one trading day, and the day before's.

    The market version of a final score. `league` is "market/<SYMBOL>": the
    scheduler treats every symbol like its own tiny league, which is how the
    original project's "this league failed" bookkeeping works for us
    unchanged.
    """

    symbol: str
    name: str
    is_index: bool
    close: float
    prev_close: float
    close_date: date
    # Today's volume, and the average over the days before it. None when
    # Yahoo didn't give us enough to say (some indexes have no volume).
    volume: int | None = None
    avg_volume: float | None = None
    # Milestones, filled in when we have the long history (see extremes()).
    record_high: bool = False  # highest close ever
    low_52w: bool = False  # lowest close in a year

    @property
    def league(self) -> str:
        return league_for(self.symbol)

    @property
    def change(self) -> float:
        return self.close - self.prev_close

    @property
    def pct(self) -> float:
        """Percent change. 100 -> 101 is +1.0."""
        return self.change / self.prev_close * 100 if self.prev_close else 0.0

    @property
    def volume_ratio(self) -> float | None:
        """Today's volume as a multiple of normal. 2.0 means twice as busy."""
        if not self.volume or not self.avg_volume:
            return None
        return self.volume / self.avg_volume


def league_for(symbol: str) -> str:
    return "market/%s" % symbol.upper()


def symbol_of(league: str) -> str:
    """market/AAPL -> AAPL"""
    return league.split("/", 1)[1]


# How many earlier days make up "normal" volume. About a month of trading.
VOLUME_LOOKBACK = 20
# Fewer earlier days than this and an average means little, so we don't
# report on volume at all.
MIN_VOLUME_HISTORY = 10


def chart_url(symbol: str, range_: str = "1mo") -> str:
    """A month: enough to always include yesterday's close, even after a long
    weekend, and enough earlier days to know what "normal volume" looks like.
    Replay asks for more."""
    return "%s/%s?range=%s&interval=1d" % (CHART_API, symbol, range_)


def full_history_url(symbol: str) -> str:
    """Every trading day since the symbol began. Asking with range=max gets
    you one point every few months instead, which misses most peaks; asking
    "from 1970 until the far future, daily" gets every day. About 1MB."""
    return "%s/%s?period1=0&period2=9999999999&interval=1d" % (CHART_API, symbol)


@dataclass(frozen=True)
class Extremes:
    """The best and worst closes before some day, to compare that day with."""

    record_close: float | None  # highest close ever
    low_52w: float | None  # lowest close in the year before; None if under a year old


def extremes(history: History, day: date) -> Extremes:
    """ELI5: before `day`, what was the highest close ever, and the lowest
    close in the past year? If `day` beats either, it's a milestone."""
    prior = [bar for bar in history.bars if bar.day < day]
    if not prior:
        return Extremes(None, None)
    year_ago = day - timedelta(days=365)
    last_year = [bar.close for bar in prior if bar.day >= year_ago]
    # A stock that listed six months ago has no "52-week" low yet.
    old_enough = prior[0].day <= year_ago
    return Extremes(
        record_close=max(bar.close for bar in prior),
        low_52w=min(last_year) if old_enough and last_year else None,
    )


def mark_milestones(quote: Quote, found: Extremes) -> Quote:
    """The same quote, with record_high / low_52w switched on if earned."""
    return replace(
        quote,
        record_high=found.record_close is not None and quote.close > found.record_close,
        low_52w=found.low_52w is not None and quote.close < found.low_52w,
    )


def _utc(seconds) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def parse_chart(payload: dict, symbol: str) -> History:
    """Turn one chart response into a History. Raises ParseError."""
    try:
        chart = payload["chart"]
        if chart.get("error"):
            raise ParseError("yahoo error for %s: %s" % (symbol, chart["error"]))
        result = chart["result"][0]
        meta = result["meta"]
        zone = ZoneInfo(meta["exchangeTimezoneName"])

        stamps = result.get("timestamp") or []
        prices = result["indicators"]["quote"][0]
        closes = prices.get("close") or []
        volumes = prices.get("volume") or [None] * len(closes)

        bars = []
        for stamp, close, volume in zip(stamps, closes, volumes):
            # Yahoo sometimes leaves a hole (null) for a day with no trades.
            # Skip it rather than pretend it was zero.
            if close is None:
                continue
            bars.append(Bar(day=_utc(stamp).astimezone(zone).date(), close=float(close),
                            volume=int(volume) if volume else None))

        regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        session_end = _utc(regular["end"]) if "end" in regular else None
        return History(
            symbol=meta.get("symbol", symbol),
            name=meta.get("shortName") or meta.get("longName") or symbol,
            is_index=meta.get("instrumentType") == "INDEX",
            bars=tuple(bars),
            market_time=_utc(meta["regularMarketTime"]),
            session_day=session_end.astimezone(zone).date() if session_end else None,
            session_end=session_end,
            price=float(meta["regularMarketPrice"]) if meta.get("regularMarketPrice") else None,
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ParseError("malformed chart for %s: %s" % (symbol, exc)) from exc


def quote_for(history: History, day: date) -> Quote | None:
    """The closing price on `day`, or None if we don't have a finished one.

    ELI5: find the bar for that day and the bar just before it, and you have
    "closed at X, up/down from Y". Return None if:
      - there is no bar for that day (holiday, or Yahoo hasn't got it yet)
      - there is no earlier bar to compare with
      - it's today's bar and trading hasn't finished: a price at 3:59pm is
        not a closing price, the same way a score at half time is not final.
    """
    index = next((i for i, bar in enumerate(history.bars) if bar.day == day), None)
    if index is None or index == 0:
        return None

    bar = history.bars[index]
    # Only the bar for Yahoo's current session can be unfinished. Comparing
    # days matters: on a Saturday the "current" session is Monday's, and
    # Friday's finished close must not look like it is still trading.
    still_trading = (
        history.session_end is not None
        and bar.day == history.session_day
        and history.market_time < history.session_end
    )
    if still_trading:
        return None

    return Quote(
        symbol=history.symbol,
        name=history.name,
        is_index=history.is_index,
        close=bar.close,
        prev_close=history.bars[index - 1].close,
        close_date=day,
        volume=bar.volume,
        avg_volume=_average_volume(history.bars[max(0, index - VOLUME_LOOKBACK):index]),
    )


def live_quote(history: History, day: date) -> Quote | None:
    """The price right now, compared with the previous day's close, for the
    `quote` command. Shaped like a Quote so it prints like one; `close` here
    means "latest price", not a finished close. None if Yahoo gave no price
    or there's no earlier day to compare with."""
    earlier = [bar for bar in history.bars if bar.day < day]
    if history.price is None or not earlier:
        return None
    return Quote(symbol=history.symbol, name=history.name, is_index=history.is_index,
                 close=history.price, prev_close=earlier[-1].close, close_date=day)


def latest_quote(history: History) -> Quote | None:
    """The most recent finished close, whatever day it was.

    ELI5: "how did it do last time the market closed?" At 8pm that's today;
    at 11am or on a Sunday it's the last trading day before.
    """
    for bar in reversed(history.bars):
        quote = quote_for(history, bar.day)
        if quote is not None:
            return quote
    return None


def _average_volume(bars) -> float | None:
    """The average volume of these days, or None if there's too little to go on."""
    volumes = [bar.volume for bar in bars if bar.volume]
    if len(volumes) < MIN_VOLUME_HISTORY:
        return None
    return sum(volumes) / len(volumes)


# -- the market's biggest movers -------------------------------------------

SCREENER_API = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
# Yahoo's ready-made lists, and how many of each to ask for. The biggest
# 100 companies catch "Nvidia fell 4%"; the day's top percentage gainers and
# losers catch a mid-sized company having an enormous day.
SCREENS = (("largest_market_cap", 100), ("day_gainers", 25), ("day_losers", 25))


@dataclass(frozen=True)
class Mover:
    """A company's move today, and what it was worth in dollars."""

    symbol: str
    name: str
    pct: float
    market_cap: float  # the whole company's value, in dollars
    day: date  # the trading day this move happened on

    @property
    def value_change(self) -> float:
        """Dollars of company value gained or lost today.

        ELI5: this is what makes a move "big". Amazon moving 2% is about
        $50 billion; a tiny company doubling might be $50 million. Ranking
        by percentage alone would put the tiny company first.
        """
        # The cap is today's (after the move), so undo the move to get
        # yesterday's value, then take the percentage of that.
        before = self.market_cap / (1 + self.pct / 100)
        return before * self.pct / 100


def screener_url(list_id: str, count: int) -> str:
    return "%s?scrIds=%s&count=%d" % (SCREENER_API, list_id, count)


def parse_screener(payload: dict) -> list:
    """One of those lists -> Movers. Raises ParseError."""
    try:
        result = payload["finance"]["result"][0]
        movers = []
        for row in result["quotes"]:
            if not row.get("marketCap"):
                continue  # funds and oddities with no company value
            zone = ZoneInfo(row.get("exchangeTimezoneName") or "America/New_York")
            movers.append(Mover(
                symbol=row["symbol"],
                name=row.get("longName") or row.get("shortName") or row["symbol"],
                pct=float(row["regularMarketChangePercent"]),
                market_cap=float(row["marketCap"]),
                day=_utc(row["regularMarketTime"]).astimezone(zone).date(),
            ))
        return movers
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ParseError("malformed screener: %s" % exc) from exc


def load_fixture(path: str | Path, symbol: str) -> History:
    """Read a saved chart from disk, for tests."""
    with open(path, encoding="utf-8") as handle:
        return parse_chart(json.load(handle), symbol)
