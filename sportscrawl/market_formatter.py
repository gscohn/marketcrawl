"""Writing the market-close message.

ELI5: given a pile of closing prices and the list of symbols one person
follows, write the text of their message, e.g.

    Fri Oct 30 close

    📉 S&P 500 7,580.07 -1.82%
    🚀 NVDA 210.40 +7.12% 🎩 🔊
    📉 AAPL 344.63 -1.23%

    Filings
    AAPL earnings release (8-K) https://www.sec.gov/...

Like the original formatter, this is a "pure function": it only looks at
what it's handed and returns text. No network, no database, no clock. That's
what makes it easy to test: same input, same output, every time.

It returns the original project's Digest object, with the same meaning:
a message to send, a partial message, an apology, or "send nothing".
"""

from __future__ import annotations

from datetime import datetime

from .edgar import Filing
from .formatter import MAX_MESSAGE, _header
from .models import Digest
from .nyse_calendar import new_york_date
from .yahoo import Quote

UP = "\N{CHART WITH UPWARDS TREND}"
DOWN = "\N{CHART WITH DOWNWARDS TREND}"
FLAT = "\N{HEAVY MINUS SIGN}"
BIG_UP = "\N{ROCKET}"
BIG_DOWN = "\N{COLLISION SYMBOL}"
BUSY = "\N{SPEAKER WITH THREE SOUND WAVES}"
RECORD_HIGH = "\N{TOP HAT}"  # dressed up: the best close it has ever had
LOW_52W = "\N{HOLE}\N{VARIATION SELECTOR-16}"  # hit bottom: lowest close in a year

# What counts as a big day. Indexes move less than single stocks (they're
# an average of hundreds), so a smaller move is already big news for them.
BIG_MOVE_STOCK = 5.0  # percent
BIG_MOVE_INDEX = 2.0  # percent
# Volume this many times the usual gets the speaker: lots of people trading
# it today, usually because of news.
BUSY_VOLUME = 2.0

# The original's two notices, reworded from scores to prices.
MISSING_DATA_LINE = "Some prices are missing."
FAILURE_NOTICE = "No prices available right now. The data source isn't responding."


def _arrow(quote) -> str:
    """Rocket or explosion for a big day, a chart line for a normal one."""
    big = BIG_MOVE_INDEX if quote.is_index else BIG_MOVE_STOCK
    if quote.pct >= big:
        return BIG_UP
    if quote.pct <= -big:
        return BIG_DOWN
    if quote.change > 0:
        return UP
    if quote.change < 0:
        return DOWN
    return FLAT


def _line(quote) -> str:
    """One line: arrow, name, price, percent change, and a speaker if busy."""
    arrow = _arrow(quote)
    # Indices go by their name ("S&P 500" reads better than "^GSPC");
    # stocks go by their ticker, which is what people type and recognise.
    label = quote.name if quote.is_index else quote.symbol
    # "{:,.2f}" means: commas every three digits, two decimal places.
    # "{:+.2f}" means: always show the sign, + or -.
    line = "%s %s %s %s%%" % (arrow, label, "{:,.2f}".format(quote.close),
                              "{:+.2f}".format(quote.pct))
    if quote.record_high:
        line += " " + RECORD_HIGH
    if quote.low_52w:
        line += " " + LOW_52W
    ratio = quote.volume_ratio
    if ratio is not None and ratio >= BUSY_VOLUME:
        line += " " + BUSY
    return line


def _filing_line(filing) -> str:
    return "%s %s %s" % (filing.symbol, filing.label, filing.url)


def _sort_key(quote):
    # Indices first (the big picture), then stocks alphabetically.
    return (not quote.is_index, quote.symbol)


def _render(lines, filing_lines, day, trailers) -> str:
    out = ["%s close" % _header(day), ""] + lines
    if filing_lines:
        out.extend(["", "Filings"] + filing_lines)
    for trailer in trailers:
        out.extend(["", trailer])
    return "\n".join(out)


def format_market_digest(events, subs, tz: str, now: datetime,
                         failed_leagues=frozenset(), day=None) -> Digest:
    """Assemble one subscriber's message. Same signature as format_digest,
    so the scheduler can call either. `tz` is unused: the market's day is
    New York's day no matter where the reader lives.

    `events` is the source's pile: prices (Quotes) and, if filings are
    switched on, SEC filings (Filings). They're sorted apart here.

    `day` is which close to report: today's unless told otherwise. A
    `test` command asks for the last finished one.
    """
    day = day or new_york_date(now)
    # (The pile can also hold the market's biggest movers, which are only
    # for the public post; they're neither, so they're ignored here.)
    quotes = [e for e in events if isinstance(e, Quote)]
    filings = [e for e in events if isinstance(e, Filing)]

    followed = {sub.league for sub in subs if sub.kind == "ticker"}
    if not followed:
        return Digest(text=None, kind="silent")

    failed = followed & set(failed_leagues)
    if failed == followed:
        # Couldn't get a single one of their prices.
        return Digest(text=FAILURE_NOTICE, kind="failure")

    mine = sorted(
        (q for q in quotes if q.league in followed and q.close_date == day),
        key=_sort_key,
    )
    if not mine:
        return Digest(text=FAILURE_NOTICE if failed else None,
                      kind="failure" if failed else "silent")

    lines = [_line(q) for q in mine]
    filing_lines = [
        _filing_line(f)
        for f in sorted((f for f in filings if f.league in followed),
                        key=lambda f: (f.symbol, f.accepted))
    ]
    trailers = [MISSING_DATA_LINE] if failed else []

    # Bluesky rejects messages over 1000 characters. Filing links are long,
    # so a busy earnings day could get there. Drop filings first (prices are
    # the point of the message), then price lines, and say how many, rather
    # than having the whole message bounce.
    text = _render(lines, filing_lines, day, trailers)
    dropped = 0
    while len(text) > MAX_MESSAGE and (filing_lines or lines):
        (filing_lines or lines).pop()
        dropped += 1
        text = _render(lines, filing_lines, day, ["+%d more" % dropped] + trailers)

    return Digest(text=text, kind="partial" if failed else "digest")
