"""Writing the market-close message.

ELI5: given a pile of closing prices and the list of symbols one person
follows, write the text of their message, e.g.

    Fri Oct 30 close

    📈 S&P 500 7,580.07 -1.82%
    📉 AAPL 344.63 -0.89%

Like the original formatter, this is a "pure function": it only looks at
what it's handed and returns text. No network, no database, no clock. That's
what makes it easy to test: same input, same output, every time.

It returns the original project's Digest object, with the same meaning:
a message to send, a partial message, an apology, or "send nothing".
"""

from __future__ import annotations

from datetime import datetime

from .formatter import MAX_MESSAGE, _header
from .models import Digest
from .nyse_calendar import new_york_date

UP = "\N{CHART WITH UPWARDS TREND}"
DOWN = "\N{CHART WITH DOWNWARDS TREND}"
FLAT = "\N{HEAVY MINUS SIGN}"

# The original's two notices, reworded from scores to prices.
MISSING_DATA_LINE = "Some prices are missing."
FAILURE_NOTICE = "No prices available right now. The data source isn't responding."


def _line(quote) -> str:
    """One line: arrow, name, price, percent change."""
    if quote.change > 0:
        arrow = UP
    elif quote.change < 0:
        arrow = DOWN
    else:
        arrow = FLAT
    # Indices go by their name ("S&P 500" reads better than "^GSPC");
    # stocks go by their ticker, which is what people type and recognise.
    label = quote.name if quote.is_index else quote.symbol
    # "{:,.2f}" means: commas every three digits, two decimal places.
    # "{:+.2f}" means: always show the sign, + or -.
    return "%s %s %s %s%%" % (arrow, label, "{:,.2f}".format(quote.close),
                             "{:+.2f}".format(quote.pct))


def _sort_key(quote):
    # Indices first (the big picture), then stocks alphabetically.
    return (not quote.is_index, quote.symbol)


def _render(lines, day, trailers) -> str:
    out = ["%s close" % _header(day), ""] + lines
    for trailer in trailers:
        out.extend(["", trailer])
    return "\n".join(out)


def format_market_digest(quotes, subs, tz: str, now: datetime,
                         failed_leagues=frozenset()) -> Digest:
    """Assemble one subscriber's message. Same signature as format_digest,
    so the scheduler can call either. `tz` is unused: the market's day is
    New York's day no matter where the reader lives."""
    day = new_york_date(now)

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
    trailers = [MISSING_DATA_LINE] if failed else []

    # Bluesky rejects messages over 1000 characters. With the 20-symbol cap
    # this can't really happen, but if it ever does, drop lines from the end
    # and say how many, rather than having the whole message bounce.
    text = _render(lines, day, trailers)
    dropped = 0
    while len(text) > MAX_MESSAGE and lines:
        lines.pop()
        dropped += 1
        text = _render(lines, day, ["+%d more" % dropped] + trailers)

    return Digest(text=text, kind="partial" if failed else "digest")
