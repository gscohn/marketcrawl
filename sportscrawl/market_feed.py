"""The daily public post.

ELI5: besides the private DMs, the bot writes one public post per trading
day, a few minutes after the close, for anyone scrolling past:

    Fri Oct 30 close

    📉 S&P 500 7,580 -1.82%
    💥 Nasdaq 25,437 -2.86%
    📉 Dow 51,770 -1.09%

    Biggest movers
    🚀 CDNA +22.2%  CPRI +16.3%  FDXF +14.9%
    💥 RDDT -29.2%  INTC -28.6%  CACC -19.3%

    Want your own stocks? DM me "help"

The trick that keeps this small: the public feed is treated as one more
subscriber. It "follows" the three indexes plus a special `feed` marker, and
the scheduler handles it exactly like a person: once a day, retries, the
too-late cutoff, silence on holidays. Only two things differ, both here:
  - the writing (a public post, max 300 characters, not a DM)
  - the delivery (a post on the bot's profile, not a DM)

Public posts are, well, public, so two extra rules:
  - never post an apology ("prices unavailable"). If there's nothing to
    say, say nothing.
  - DRY RUN until switched on: posts are printed, not published.
"""

from __future__ import annotations

from . import market_repo, repo
from .formatter import _header
from .market_formatter import _arrow, format_market_digest
from .models import Digest, Sub
from .nyse_calendar import new_york_date
from .yahoo import Mover, Quote, league_for

# The feed's pretend subscriber ID. Not a real Bluesky DID (those start
# with "did:"), so no DM can ever be sent to it or come from it.
FEED_DID = "feed:public"
FEED_KIND = "feed"
FEED_LEAGUE = "feed/public"

# The indexes in the post, in this order, with short names to save space.
FEED_INDEXES = (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"))
MOVERS_EACH_WAY = 3

# Bluesky's limit for a post (it counts characters as people see them, so
# counting code points, which is what len() does, is on the safe side).
POST_LIMIT = 300
CALL_TO_ACTION = 'Want your own stocks? DM me "help"'


# -- switching it on and off -----------------------------------------------

def enable(connection, now, pepper) -> None:
    """Create the feed's pretend subscriber. Safe to run twice."""
    repo.create_subscriber(connection, FEED_DID, now, pepper, tz=market_repo.MARKET_TZ)
    for symbol, _name in FEED_INDEXES:
        market_repo.add_ticker(connection, FEED_DID, symbol)
    repo.add_sub(connection, FEED_DID, FEED_KIND, FEED_LEAGUE)


def disable(connection, now, pepper) -> None:
    repo.delete_subscriber(connection, FEED_DID, now, pepper)


def is_feed(subs) -> bool:
    return any(sub.kind == FEED_KIND for sub in subs)


# -- writing the post ------------------------------------------------------

def _index_line(quote: Quote, name: str) -> str:
    # Whole numbers: in a 300-character post, "7,580" says enough.
    return "%s %s %s %s%%" % (_arrow(quote), name, "{:,.0f}".format(quote.close),
                              "{:+.2f}".format(quote.pct))


def _movers_line(arrow: str, movers) -> str:
    return arrow + " " + "  ".join("%s %s%%" % (m.symbol, "{:+.1f}".format(m.pct))
                                   for m in movers)


def format_feed_post(events, day) -> Digest:
    """The public post for `day`, or silent if there's nothing worth posting."""
    quotes = {e.symbol: e for e in events
              if isinstance(e, Quote) and e.close_date == day}
    index_lines = [_index_line(quotes[symbol], name)
                   for symbol, name in FEED_INDEXES if symbol in quotes]
    if not index_lines:
        # No index prices: rather than a half-empty or apologetic post in
        # public, skip the day.
        return Digest(text=None, kind="silent")

    movers = [e for e in events if isinstance(e, Mover) and e.day == day]
    gainers = sorted((m for m in movers if m.pct > 0), key=lambda m: -m.pct)
    losers = sorted((m for m in movers if m.pct < 0), key=lambda m: m.pct)

    # Start with three each way and drop from the end until it fits.
    for count in range(MOVERS_EACH_WAY, -1, -1):
        lines = ["%s close" % _header(day), ""] + index_lines
        if count and (gainers or losers):
            lines += ["", "Biggest movers"]
            if gainers:
                lines.append(_movers_line("\N{ROCKET}", gainers[:count]))
            if losers:
                lines.append(_movers_line("\N{COLLISION SYMBOL}", losers[:count]))
        lines += ["", CALL_TO_ACTION]
        text = "\n".join(lines)
        if len(text) <= POST_LIMIT:
            return Digest(text=text, kind="digest")
    return Digest(text=None, kind="silent")


def format_for(events, subs, tz, now, failed_leagues=frozenset(), day=None):
    """The scheduler's formatter: the public post for the feed, the usual
    DM for everyone else. Same signature as format_market_digest."""
    if is_feed(subs):
        return format_feed_post(events, day or new_york_date(now))
    return format_market_digest(events, subs, tz, now, failed_leagues, day=day)


# -- delivering it ---------------------------------------------------------

def dry_run_poster(text: str) -> None:
    print("--- public post (DRY RUN: not posted)\n%s" % text, flush=True)


class FeedRouter:
    """The scheduler has one "send" for everyone. This one sends the feed's
    message to the poster and everybody else's to the normal DM sender."""

    def __init__(self, sender, poster=dry_run_poster):
        self._sender = sender
        self._poster = poster

    def __call__(self, did: str, text: str) -> None:
        if did == FEED_DID:
            self._poster(text)
        else:
            self._sender(did, text)


def feed_subs() -> list:
    """What the feed follows, as Subs (for previews)."""
    return [Sub(kind=market_repo.TICKER, league=league_for(s)) for s, _ in FEED_INDEXES] + [
        Sub(kind=FEED_KIND, league=FEED_LEAGUE)
    ]
