"""The daily public post.

ELI5: besides the private DMs, the bot writes one public post per trading
day, a few minutes after the close, for anyone scrolling past:

    Fri Oct 30 close
    📉 S&P 500 -1.82%
    💥 Nasdaq -2.86%
    📉 Dow -1.09%

    Biggest swings in company value
    📉 $NVDA Nvidia 201.34 -4.2% (-$218B)
    💥 $INTC Intel 24.10 -28.6% (-$140B)
    💥 $TSM TSMC 281.50 -5.9% (-$133B)

    DM me to get private stock updates daily

Short on purpose: a glance in a feed, not a report. One thing per line,
because on a phone a long line wraps somewhere unpredictable. Company
names are the everyday ones ("TSMC", not "Taiwan Semiconductor
Manufacturing Company Limited") so the post fits Bluesky's 300 characters; if it
still doesn't fit, the smallest movers are dropped until it does (with
a price on each mover, that's usually four of them). The movers are ranked
by dollars of company value moved, not by percentage, so Nvidia falling 4%
outranks an obscure company doubling. Symbols are written as cashtags
($NVDA), so the post turns up when people search Bluesky for that stock.

The trick that keeps this small: the public feed is treated as one more
subscriber. It "follows" the three indexes plus a special `feed` marker, and
the scheduler handles it exactly like a person: once a day, retries, the
too-late cutoff, silence on holidays. Only two things differ, both here:
  - the writing (a public post, max 300 characters, not a DM)
  - the delivery (a post on the bot's profile, not a DM)

On days when big companies report earnings, a second post follows about an
hour after the close (75 minutes after the bell), by a second pretend
subscriber, the earnings feed:

    Mon Nov 2 earnings
    $AAPL Apple earnings release (8-K) sec.gov/Archives/edg...
    $MSFT Microsoft earnings release (8-K) sec.gov/Archives/edg...

It's the DM's "Filings" line (with a cashtag and the everyday name) for
every company worth $100B or more that put out its results since the last
one, biggest first. It waits that long because most companies report just
after the bell, after the price post has gone. A busy day becomes a short
thread rather than a cut-off list.

Public posts are, well, public, so two extra rules:
  - never post an apology ("prices unavailable"). If there's nothing to
    say, say nothing.
  - DRY RUN unless switched on: `run_market.py --post` publishes the
    post (market_post.py); without it, it's only printed.
"""

from __future__ import annotations

import re

from . import market_repo, repo
from .formatter import _header
from .edgar import MajorEarnings
from .market_formatter import BIG_DOWN, BIG_MOVE_STOCK, BIG_UP, DOWN, UP, _arrow, format_market_digest
from .market_post import POST_BREAK, cashtag, shown_length
from .market_schedule import classify_market, earnings_due
from .models import Digest, Sub
from .nyse_calendar import new_york_date
from .yahoo import Mover, Quote, league_for

# The feed's pretend subscriber ID. Not a real Bluesky DID (those start
# with "did:"), so no DM can ever be sent to it or come from it.
FEED_DID = "feed:public"
FEED_KIND = "feed"
FEED_LEAGUE = "feed/public"
# The earnings post's pretend subscriber: same idea, its own clock.
EARNINGS_DID = "feed:earnings"
EARNINGS_LEAGUE = "feed/earnings"
FEED_DIDS = (FEED_DID, EARNINGS_DID)
# At most this many posts in an earnings thread; past that, "+N more".
MAX_EARNINGS_POSTS = 4

# The indexes in the post, in this order, with short names to save space.
FEED_INDEXES = (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow"))
TOP_MOVERS = 5

# Company names longer than this get cut at a word boundary.
NAME_CHARS = 14
# What people actually call the companies that most often top the list.
# Anything not here gets its official name shortened by short_name().
EVERYDAY_NAMES = {
    "AAPL": "Apple", "MSFT": "Microsoft", "NVDA": "Nvidia", "AMZN": "Amazon",
    "GOOGL": "Alphabet", "GOOG": "Alphabet", "META": "Meta", "TSLA": "Tesla",
    "AVGO": "Broadcom", "TSM": "TSMC", "BRK-A": "Berkshire", "BRK-B": "Berkshire",
    "LLY": "Eli Lilly", "JPM": "JPMorgan", "V": "Visa", "MA": "Mastercard",
    "WMT": "Walmart", "XOM": "Exxon", "UNH": "UnitedHealth", "ORCL": "Oracle",
    "NFLX": "Netflix", "COST": "Costco", "PLTR": "Palantir", "SPCX": "SpaceX",
    "JNJ": "J&J", "PG": "P&G", "HD": "Home Depot", "BAC": "BofA",
    "KO": "Coca-Cola", "CRM": "Salesforce", "NVO": "Novo Nordisk",
    "BABA": "Alibaba", "TM": "Toyota", "ASML": "ASML", "AMD": "AMD",
    "INTC": "Intel", "MU": "Micron", "ABBV": "AbbVie", "CVX": "Chevron",
}
# Words that tell you what kind of company it is, not which one.
_NAME_ENDINGS = re.compile(
    r"[,.]?\s+(Inc|Incorporated|Corp|Corporation|Co|Company|Ltd|Limited|plc|PLC|"
    r"Holdings?|Group|S\.?A|N\.?V|AG|SE|Class [A-Z])\.?$")

# Bluesky's limit for a post (it counts characters as people see them, so
# counting code points, which is what len() does, is on the safe side).
POST_LIMIT = 300
CALL_TO_ACTION = "DM me to get private stock updates daily"


# -- switching it on and off -----------------------------------------------

def enable(connection, now, pepper) -> None:
    """Create both feeds' pretend subscribers (prices, then earnings). Safe
    to run twice: one already there is left alone."""
    if repo.get_subscriber(connection, FEED_DID) is None:
        repo.create_subscriber(connection, FEED_DID, now, pepper, tz=market_repo.MARKET_TZ)
    for symbol, _name in FEED_INDEXES:
        market_repo.add_ticker(connection, FEED_DID, symbol)
    repo.add_sub(connection, FEED_DID, FEED_KIND, FEED_LEAGUE)
    # The earnings feed follows no symbols: what it reports on comes from
    # the market's biggest companies, whoever follows them.
    if repo.get_subscriber(connection, EARNINGS_DID) is None:
        repo.create_subscriber(connection, EARNINGS_DID, now, pepper, tz=market_repo.MARKET_TZ)
    repo.add_sub(connection, EARNINGS_DID, FEED_KIND, EARNINGS_LEAGUE)


def disable(connection, now, pepper) -> None:
    for did in FEED_DIDS:
        if repo.get_subscriber(connection, did) is not None:
            repo.delete_subscriber(connection, did, now, pepper)


def is_feed(subs) -> bool:
    return any(sub.kind == FEED_KIND for sub in subs)


def is_earnings_feed(subs) -> bool:
    return any(sub.kind == FEED_KIND and sub.league == EARNINGS_LEAGUE for sub in subs)


def classify_for(row, now):
    """The scheduler's "is it due?" question: the earnings feed is due later
    than everyone else; everyone else keeps the usual market clock."""
    if row["did"] == EARNINGS_DID:
        return classify_market(row, now, due=earnings_due)
    return classify_market(row, now)


# -- writing the post ------------------------------------------------------

def _index_part(quote: Quote, name: str) -> str:
    # Just the percentage: an index's level ("7,541.40") means little at a
    # glance, and leaving it out makes room for another mover.
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


# Don't end on a joining word ("Eli Lilly and").
_JOINER_AT_END = re.compile(r"\s+(and|&|of|the)$", re.IGNORECASE)


def short_name(name: str) -> str:
    """'NVIDIA Corporation' -> 'NVIDIA'; 'Taiwan Semiconductor Manufacturing
    Company Limited' -> 'Taiwan Semi'."""
    previous = None
    while previous != name:  # peel endings off one at a time
        previous, name = name, _NAME_ENDINGS.sub("", name).strip(" ,")
        name = _JOINER_AT_END.sub("", name)
    if len(name) <= NAME_CHARS:
        return name
    kept = ""
    for word in name.split():
        trial = (kept + " " + word).strip()
        if len(trial) > NAME_CHARS:
            break
        kept = trial
    kept = _JOINER_AT_END.sub("", kept)
    # A very long first word: cut it, so one name can't crowd out a mover.
    return kept or name[:NAME_CHARS]


def company_name(symbol: str, official: str) -> str:
    """The name to show after the symbol, or "" when it would only repeat
    the symbol (AMD is just AMD)."""
    name = EVERYDAY_NAMES.get(symbol) or short_name(official)
    return "" if name.upper() == symbol else name


def display_name(mover: Mover) -> str:
    return company_name(mover.symbol, mover.name)


def _mover_line(mover: Mover) -> str:
    if mover.pct >= BIG_MOVE_STOCK:
        arrow = BIG_UP
    elif mover.pct <= -BIG_MOVE_STOCK:
        arrow = BIG_DOWN
    else:
        arrow = UP if mover.pct > 0 else DOWN
    price = "{:,.2f}".format(mover.price) if mover.price else None
    who = " ".join(filter(None, (cashtag(mover.symbol), display_name(mover), price)))
    return "%s %s %s%% (%s)" % (arrow, who, "{:+.1f}".format(mover.pct),
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
    # Drop the smallest movers until it fits; with none left and still too
    # long (it never should be), skip the day rather than post it cut off.
    while True:
        lines = ["%s close" % _header(day)] + indexes
        if movers:
            lines += ["", "Biggest swings in company value"] + [_mover_line(m) for m in movers]
        lines += ["", CALL_TO_ACTION]
        text = "\n".join(lines)
        if len(text) <= POST_LIMIT:
            return Digest(text=text, kind="digest")
        if not movers:
            return Digest(text=None, kind="silent")
        movers = movers[:-1]


def _earnings_line(report: MajorEarnings) -> str:
    """The DM's filings line ("AAPL earnings release (8-K) <link>"), with a
    cashtag and the company's everyday name added for strangers."""
    filing = report.filing
    who = " ".join(filter(None, (cashtag(filing.symbol),
                                 company_name(filing.symbol, report.name))))
    return "%s %s %s" % (who, filing.label, filing.url)


def format_earnings_post(events, day) -> Digest:
    """The public earnings post for `day`: one line per big company that
    reported, biggest first, as a thread if one post can't hold them all.
    Silent on a day with none."""
    reports, seen = [], set()
    for report in sorted((e for e in events if isinstance(e, MajorEarnings)),
                         key=lambda r: (-r.market_cap, r.filing.symbol)):
        if report.filing.url not in seen:
            seen.add(report.filing.url)
            reports.append(report)
    if not reports:
        return Digest(text=None, kind="silent")

    lines = [_earnings_line(r) for r in reports]
    posts = [["%s earnings" % _header(day)]]
    for number, line in enumerate(lines):
        if shown_length("\n".join(posts[-1] + [line])) <= POST_LIMIT:
            posts[-1].append(line)
        elif len(posts) < MAX_EARNINGS_POSTS:
            posts.append([line])  # this post is full: start the next one
        else:
            # Out of posts. End the last one with a count of the rest,
            # making room for it if need be.
            left = len(lines) - number
            while shown_length("\n".join(posts[-1] + ["+%d more" % left])) > POST_LIMIT:
                posts[-1].pop()
                left += 1
            posts[-1].append("+%d more" % left)
            break
    return Digest(text=POST_BREAK.join("\n".join(p) for p in posts), kind="digest")


def format_for(events, subs, tz, now, failed_leagues=frozenset(), day=None):
    """The scheduler's formatter: the public posts for the feeds, the usual
    DM for everyone else. Same signature as format_market_digest."""
    if is_earnings_feed(subs):
        return format_earnings_post(events, day or new_york_date(now))
    if is_feed(subs):
        return format_feed_post(events, day or new_york_date(now))
    return format_market_digest(events, subs, tz, now, failed_leagues, day=day)


# -- delivering it ---------------------------------------------------------

def dry_run_poster(text: str) -> None:
    for part in text.split(POST_BREAK):
        print("--- public post (DRY RUN: not posted)\n%s" % part, flush=True)


class FeedRouter:
    """The scheduler has one "send" for everyone. This one sends the feed's
    message to the poster and everybody else's to the normal DM sender."""

    def __init__(self, sender, poster=dry_run_poster):
        self._sender = sender
        self._poster = poster

    def __call__(self, did: str, text: str) -> None:
        if did in FEED_DIDS:
            self._poster(text)
        else:
            self._sender(did, text)


def feed_subs() -> list:
    """What the feed follows, as Subs (for previews)."""
    return [Sub(kind=market_repo.TICKER, league=league_for(s)) for s, _ in FEED_INDEXES] + [
        Sub(kind=FEED_KIND, league=FEED_LEAGUE)
    ]
