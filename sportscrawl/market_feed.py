"""The daily public post.

ELI5: besides the private DMs, the bot writes one public post per trading
day, a few minutes after the close, for anyone scrolling past:

    Fri Oct 30 close
    📉 S&P 500 -1.82%  💥 Nasdaq -2.86%  📉 Dow -1.09%

    Biggest swings in company value
    📉 NVDA -4.2% (-$209B)
    💥 TSM -5.9% (-$125B)
    💥 SPCX -6.4% (-$117B)

    DM me "help" for your own stocks

Short on purpose: a glance in a feed, not a report. The movers are ranked
by dollars of company value moved, not by percentage, so Nvidia falling 4%
outranks an obscure company doubling.

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
from .market_formatter import BIG_DOWN, BIG_MOVE_STOCK, BIG_UP, DOWN, UP, _arrow, format_market_digest
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
TOP_MOVERS = 3

# Bluesky's limit for a post (it counts characters as people see them, so
# counting code points, which is what len() does, is on the safe side).
POST_LIMIT = 300
CALL_TO_ACTION = 'DM me "help" for your own stocks'


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

def _index_part(quote: Quote, name: str) -> str:
    # Just the percentage: in a glance-sized post, the change is the news.
    return "%s %s %s%%" % (_arrow(quote), name, "{:+.2f}".format(quote.pct))


def _dollars(amount: float) -> str:
    """-2.09e11 -> "-$209B", 8.5e8 -> "+$850M"."""
    sign = "+" if amount >= 0 else "-"
    amount = abs(amount)
    if amount >= 1e12:
        return "%s$%.1fT" % (sign, amount / 1e12)
    if amount >= 1e9:
        return "%s$%.0fB" % (sign, amount / 1e9)
    return "%s$%.0fM" % (sign, amount / 1e6)


def _mover_line(mover: Mover) -> str:
    if mover.pct >= BIG_MOVE_STOCK:
        arrow = BIG_UP
    elif mover.pct <= -BIG_MOVE_STOCK:
        arrow = BIG_DOWN
    else:
        arrow = UP if mover.pct > 0 else DOWN
    return "%s %s %s%% (%s)" % (arrow, mover.symbol, "{:+.1f}".format(mover.pct),
                                _dollars(mover.value_change))


def top_movers(events, day, count=TOP_MOVERS) -> list:
    """The day's biggest swings in company value, up or down.

    The same company can turn up twice: on more than one of Yahoo's lists,
    or as two share classes (GOOG and GOOGL are both Alphabet). Keep each
    company once.
    """
    movers = sorted((e for e in events if isinstance(e, Mover) and e.day == day),
                    key=lambda m: -abs(m.value_change))
    chosen, seen = [], set()
    for mover in movers:
        if mover.symbol in seen or mover.name in seen:
            continue
        seen.update((mover.symbol, mover.name))
        chosen.append(mover)
        if len(chosen) == count:
            break
    return chosen


def format_feed_post(events, day) -> Digest:
    """The public post for `day`, or silent if there's nothing worth posting."""
    quotes = {e.symbol: e for e in events
              if isinstance(e, Quote) and e.close_date == day}
    indexes = [_index_part(quotes[symbol], name)
               for symbol, name in FEED_INDEXES if symbol in quotes]
    if not indexes:
        # No index prices: rather than a half-empty or apologetic post in
        # public, skip the day.
        return Digest(text=None, kind="silent")

    movers = top_movers(events, day)
    lines = ["%s close" % _header(day), "  ".join(indexes)]
    if movers:
        lines += ["", "Biggest swings in company value"] + [_mover_line(m) for m in movers]
    lines += ["", CALL_TO_ACTION]
    text = "\n".join(lines)
    if len(text) > POST_LIMIT:
        return Digest(text=None, kind="silent")
    return Digest(text=text, kind="digest")


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
