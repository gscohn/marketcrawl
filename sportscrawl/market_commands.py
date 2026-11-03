"""Understanding DMs sent to the market bot.

ELI5: there's no website to pick your stocks. You just message the bot:

    add AAPL            -> follow Apple
    add MSFT sp500      -> follow Microsoft and the S&P 500 index
    remove AAPL         -> stop following Apple
    list                -> what am I following?
    test                -> send my closing-prices message now
    help                -> the list of commands
    about               -> where this bot comes from
    stop / start        -> pause / resume messages
    delete              -> forget everything about me

This class takes one incoming message and returns the text to reply with. It
never sends anything itself; the poller does that. Returning text instead of
sending it is the original author's trick for making every command testable
without a network or a Bluesky account.

It builds on the original CommandHandler: anything that isn't one of our new
commands (stop, start, delete, delete confirm) is passed straight through to
the original code, so those keep behaving exactly as the author designed.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime, timedelta

from . import market_repo, repo
from .commands import DELETE_PROMPT, STARTED, CommandHandler, normalize
from .formatter import MAX_MESSAGE
from .http import FetchError
from .market_formatter import _line, _sort_key, format_market_digest
from .models import Sub
from .formatter import _header
from .nyse_calendar import NEW_YORK, is_open
from .yahoo import league_for

log = logging.getLogger("sportscrawl.market_commands")

# Most symbols anyone may follow. 20 lines comfortably fits Bluesky's
# 1000-character message limit, so nobody's message ever gets cut short.
MAX_TICKERS = 20

# What a symbol may look like: 1-10 of capital letters, digits, and the few
# punctuation marks Yahoo uses. ^GSPC (an index), BRK-B (a share class),
# EURUSD=X (a currency pair). At least one letter or digit, so ".." can't
# sneak into a Yahoo web address.
SYMBOL = re.compile(r"^(?=.*[A-Z0-9])[A-Z0-9.\-\^=]{1,10}$")

# Guards against one huge message. A stranger could otherwise send `add`
# followed by hundreds of made-up symbols: hundreds of Yahoo lookups, and a
# reply too long for Bluesky to accept, which the poller would retry forever.
# So: read at most MAX_TICKERS words, give up after a few lookups that find
# nothing, never echo more than a short piece of each word, and trim the
# reply to fit.
MAX_FAILED_LOOKUPS = 5
ECHO_CHARS = 12

# Friendly names for the three indexes people ask for most.
ALIASES = {
    "SP500": "^GSPC",
    "S&P": "^GSPC",
    "S&P500": "^GSPC",
    "SPX": "^GSPC",
    "DOW": "^DJI",
    "DJIA": "^DJI",
    "NASDAQ": "^IXIC",
}

ADD_WORDS = frozenset({"add", "follow", "track"})
REMOVE_WORDS = frozenset({"remove", "rm", "drop", "unfollow", "untrack"})
LIST_WORDS = frozenset({"list", "ls", "mine"})
HELP_WORDS = frozenset({"help", "commands", "?"})
ABOUT_WORDS = frozenset({"about", "credits", "source"})
TEST_WORDS = frozenset({"test", "preview"})
QUOTE_WORDS = frozenset({"quote", "q", "price"})

# `test` and `quote` download prices, so they're rationed (see Ration).
# test: once per person every ten minutes (the answer can't change faster
# than that anyway: it's the last close), 30 an hour for everyone together.
TEST_COOLDOWN = timedelta(minutes=10)
TEST_HOURLY_LIMIT = 30
# quote: once a minute per person. The shared limit counts symbols, not
# messages, because each symbol is a download: 600 an hour across everyone.
QUOTE_COOLDOWN = timedelta(minutes=1)
QUOTE_HOURLY_SYMBOLS = 600
MAX_QUOTE_SYMBOLS = 5

HELP = """MarketCrawl sends one message after the US stock market closes, with the closing prices of the symbols you follow, plus any earnings releases and quarterly or annual reports they filed.

add AAPL - follow a symbol (or several: add AAPL MSFT)
remove AAPL - stop following one
list - what you follow
test - get your message now (latest close)
quote - prices now for everything you follow
quote AAPL - price now for one (or several: quote AAPL MSFT)
stop - pause messages
start - resume messages
delete - remove all your data
about - where this bot comes from
privacy - what we keep
help - this list

Indexes: sp500, dow, nasdaq

\N{CHART WITH UPWARDS TREND} up  \N{CHART WITH DOWNWARDS TREND} down  \N{ROCKET}/\N{COLLISION SYMBOL} big day (5%+, or 2%+ for an index)  \N{TOP HAT} all-time high  \N{HOLE}\N{VARIATION SELECTOR-16} 52-week low  \N{SPEAKER WITH THREE SOUND WAVES} twice the usual volume

A spin-off of SportsCrawl (@sportscrawl.app) by cruftbox."""

# The credit, in full. MarketCrawl is built on SportsCrawl's code, and
# saying so plainly is both the MIT license's one requirement and good
# manners.
ABOUT = """MarketCrawl is a spin-off of SportsCrawl (@sportscrawl.app), the daily sports scores bot by cruftbox. It runs on SportsCrawl's own code, reworked for stocks: the scheduling, the DM handling and the care about never messaging you twice all come from there.

If you like scores as much as stocks, follow @sportscrawl.app.

Prices: Yahoo Finance. Filings: SEC EDGAR.
Source: https://github.com/gscohn/marketcrawl"""

ADDED_OUTRO = "You'll get closing prices after each market close. Send 'test' to see one now."

# A repeat unknown message inside the hour gets this instead of the full
# help. The original project's reason, kept: a full reply to every message
# from another bot is an endless loop.
NUDGE = "Commands: add, remove, list, test, quote, stop, help"
HELP_THROTTLE = timedelta(hours=1)

PRIVACY = """We keep: your Bluesky ID, the symbols you follow, and when you were last sent a message. Messages you send are read for commands and not stored. delete removes everything."""

# Two of the original replies talk about sports settings. Same meaning,
# market wording.
REWORDED = {
    DELETE_PROMPT: """This removes everything: the symbols you follow and your settings. It cannot be undone.

Send: delete confirm

If you only want the messages to stop, send stop instead. That keeps your symbols.""",
    STARTED: "Messages resumed. Your next one arrives after the next market close.",
}


class Ration:
    """A simple fairness rule for commands that cost a download.

    ELI5: like a playground slide. Each person has to wait their turn
    (`cooldown`) before going again, and there's a limit on how many turns
    everyone together gets in an hour (`hourly`), so a crowd of accounts
    can't use the bot to hammer Yahoo or flood replies. Some turns cost more
    than one (a quote of five symbols costs five).

    Kept in memory, not the database: a restart forgetting it only means
    someone may go a little early, which is harmless.
    """

    def __init__(self, cooldown: timedelta, hourly: int):
        self._cooldown = cooldown
        self._hourly = hourly
        self._last = {}  # person -> when they last went
        self._recent = deque()  # (when, cost) for the last hour, everyone

    def take(self, who, now: datetime, cost: int = 1):
        """None if they may go (and counts it), otherwise why not:
        ("wait", timedelta) or ("busy", None)."""
        last = self._last.get(who)
        if last is not None and now - last < self._cooldown:
            return "wait", self._cooldown - (now - last)
        while self._recent and now - self._recent[0][0] >= timedelta(hours=1):
            self._recent.popleft()
        if sum(c for _, c in self._recent) + cost > self._hourly:
            return "busy", None
        # Counted before the download, so a failed one still uses a turn.
        self._last[who] = now
        self._recent.append((now, cost))
        # Forget anyone whose wait is long over, so this never grows.
        for old in [w for w, t in self._last.items() if now - t >= self._cooldown]:
            del self._last[old]
        return None


def _wait_text(wait: timedelta) -> str:
    seconds = int(wait.total_seconds())
    if seconds < 60:
        return "%d sec" % max(1, seconds)
    return "%d min" % -(-seconds // 60)  # round up


def _time_text(when: datetime) -> str:
    """'2:31pm ET'."""
    local = when.astimezone(NEW_YORK)
    return "%d:%02d%s ET" % ((local.hour - 1) % 12 + 1, local.minute,
                             "am" if local.hour < 12 else "pm")


def clean_symbol(word: str) -> str:
    """'$aapl,' -> 'AAPL', 'sp500' -> '^GSPC'. Might still be invalid."""
    word = word.strip().upper().lstrip("$").rstrip(".,;:!?")
    return ALIASES.get(word, word)


def parse(text: str):
    """Split a message into (verb, [symbols]).

    We can't use the original normalize() for the symbols: it removes all
    punctuation, which would turn ^GSPC into GSPC (a different thing) and
    BRK.B into "brk b" (two things). So the verb gets normalised and the
    symbols are read from the raw text.
    """
    words = re.split(r"[\s,]+", (text or "").strip())
    words = [w for w in words if w]
    if not words:
        return "", []
    verb = normalize(words[0]) or words[0]
    return verb, [clean_symbol(w) for w in words[1:]]


class MarketCommandHandler(CommandHandler):
    def __init__(self, connection, pepper, lookup, base_url="https://localhost",
                 latest=None, live=None):
        # base_url only matters to commands we don't use (the web config
        # link), but the original constructor wants one.
        super().__init__(connection, pepper, base_url)
        # lookup(symbol, now) -> something with a `.name`, or None if there's
        # no such symbol; raises FetchError if Yahoo can't be reached.
        # Passed in, so tests can fake Yahoo.
        self._lookup = lookup
        # latest(symbols, now) -> the last finished close for each, for
        # `test`. Optional: without it, `test` says prices are unavailable.
        self._latest = latest
        # live(symbols, now) -> [(quote, time of last trade)], for `quote`.
        # Optional: without it, `quote` says prices are unavailable.
        self._live = live
        self._tests = Ration(TEST_COOLDOWN, TEST_HOURLY_LIMIT)
        self._quotes = Ration(QUOTE_COOLDOWN, QUOTE_HOURLY_SYMBOLS)

    def handle(self, message, now: datetime) -> str:
        did = message.sender_did
        verb, symbols = parse(message.text)
        too_many = len(symbols) > MAX_TICKERS
        symbols = symbols[:MAX_TICKERS]

        kind = _kind(verb)
        if kind != "unknown":
            # Log which command, never the message itself. The privacy
            # promise above depends on this line.
            log.info("command did=%s matched=%s", did, kind)
            self._first_contact(message, now)

            if kind in ("add", "remove"):
                reply = (self._add(did, symbols, now) if kind == "add"
                         else self._remove(did, symbols, now))
                if too_many:
                    reply += "\n(Only the first %d were read.)" % MAX_TICKERS
                return _fit(reply)
            if kind == "list":
                return self._list(did)
            if kind == "test":
                return self._test(did, now)
            if kind == "quote":
                return self._quote(did, symbols, now)
            if kind == "privacy":
                return PRIVACY
            if kind == "about":
                return ABOUT
            # help, plus the original's web-page commands (config, admin),
            # which mean nothing here.
            repo.set_help_sent(self._db, message.convo_id, now)
            return HELP

        # stop, start, delete, delete confirm, and anything unrecognised go
        # to the original handler.
        reply = super().handle(message, now)
        return REWORDED.get(reply, reply)

    # -- the original calls this for anything it doesn't recognise ---------

    def _help_reply(self, convo_id, did, now):
        previous = repo.help_sent_at(self._db, convo_id)
        repo.set_help_sent(self._db, convo_id, now)
        if previous and previous > repo.utc_string(now - HELP_THROTTLE):
            return NUDGE
        return HELP

    # -- our commands ------------------------------------------------------

    def _first_contact(self, message, now):
        """What the original handle() does before any command: create the
        subscriber if new, and cancel a pending delete (typing anything else
        after `delete` means you changed your mind)."""
        if repo.get_subscriber(self._db, message.sender_did) is None:
            repo.create_subscriber(self._db, message.sender_did, now, self._pepper)
        pending = repo.delete_pending_until(self._db, message.convo_id)
        if pending and pending > repo.utc_string(now):
            repo.set_delete_pending(self._db, message.convo_id, None)

    def _find(self, symbol, now):
        """Look a symbol up, returning (the spelling that worked, its info).

        People write share classes with a dot (BRK.B), Yahoo with a dash
        (BRK-B). But a dot is also real on Yahoo (SHOP.TO is Shopify in
        Toronto), so try it as typed first, then with the dash.
        """
        found = self._lookup(symbol, now)
        if found is None and "." in symbol:
            dashed = symbol.replace(".", "-")
            found = self._lookup(dashed, now)
            if found is not None:
                return dashed, found
        return symbol, found

    def _add(self, did, symbols, now) -> str:
        if not symbols:
            return "Which symbol? For example: add AAPL"

        following = set(market_repo.tickers_for(self._db, did))
        lines = []
        added = 0
        failed = 0
        for symbol in dict.fromkeys(symbols):  # drops repeats, keeps order
            if failed >= MAX_FAILED_LOOKUPS:
                lines.append("Stopped after %d that didn't work. Check the spelling." % failed)
                break
            if not SYMBOL.match(symbol):
                lines.append("%s doesn't look like a symbol." % _echo(symbol))
                continue
            if symbol in following:
                lines.append("Already following %s." % symbol)
                continue
            if len(following) >= MAX_TICKERS:
                lines.append("You're at the limit of %d. Remove one first." % MAX_TICKERS)
                break
            try:
                typed = symbol
                symbol, found = self._find(symbol, now)
            except FetchError:
                failed += 1
                lines.append("Couldn't check %s right now. Try again later." % typed)
                continue
            if found is None:
                failed += 1
                lines.append("Couldn't find %s." % typed)
                continue
            if symbol in following:
                lines.append("Already following %s." % symbol)
                continue
            market_repo.add_ticker(self._db, did, symbol)
            following.add(symbol)
            added += 1
            # Echo the name back, so a typo (AAPL vs APPL) is obvious.
            lines.append("Added %s (%s)." % (symbol, found.name))

        if added:
            # The scheduler skips anyone without a timezone, so the first
            # successful add is what switches delivery on.
            market_repo.ensure_market_tz(self._db, did)
            # For the operator's daily summary: how many, never which.
            repo.record_event(self._db, now, "ticker_add", did, self._pepper,
                              detail={"count": added})
            lines.append("")
            lines.append(ADDED_OUTRO)
        return "\n".join(lines)

    def _test(self, did, now) -> str:
        """Send this person's message right now, built from the last
        finished close, exactly as the after-close message would look.

        It doesn't count as that day's message: the regular one still
        arrives after the next close. Rationed (see TEST_COOLDOWN)."""
        symbols = market_repo.tickers_for(self._db, did)
        if not symbols:
            return "You're not following anything yet. Try: add AAPL"

        refused = self._tests.take(did, now)
        if refused:
            reason, wait = refused
            if reason == "wait":
                return "You can test again in %s." % _wait_text(wait)
            return "Lots of tests right now. Try again in a little while."

        try:
            report = self._latest_report(symbols, now)
        except FetchError:
            report = None
        if report is None or len(report) > MAX_MESSAGE:
            return "Couldn't get prices right now. Try again later."
        return report

    def _quote(self, did, symbols, now) -> str:
        """Prices right now while the market's open; the last close when
        it's shut. `quote` on its own means "everything I follow"."""
        if not symbols:
            symbols = market_repo.tickers_for(self._db, did)
            if not symbols:
                return "Which symbol? For example: quote AAPL"
        else:
            symbols = list(dict.fromkeys(symbols))[:MAX_QUOTE_SYMBOLS]
            bad = [s for s in symbols if not SYMBOL.match(s)]
            if bad:
                return "%s doesn't look like a symbol." % _echo(bad[0])

        refused = self._quotes.take(did, now, cost=len(symbols))
        if refused:
            reason, wait = refused
            if reason == "wait":
                return "You can quote again in %s." % _wait_text(wait)
            return "Lots of quotes right now. Try again in a little while."

        try:
            found = self._live(symbols, now) if self._live else []
        except FetchError:
            found = []
        if not found:
            return "Couldn't get prices right now. Try again later."
        quotes = sorted((q for q, _ in found), key=_sort_key)
        if is_open(now):
            header = "Prices at %s" % _time_text(max(t for _, t in found))
        else:
            header = "%s close" % _header(max(q.close_date for q in quotes))
        lines = [header, ""] + [_line(q) for q in quotes]
        missing = [s for s in symbols if s not in {q.symbol for q in quotes}]
        if missing:
            lines += ["", "Couldn't find: %s" % " ".join(_echo(s) for s in missing)]
        return _fit("\n".join(lines))

    def _latest_report(self, symbols, now):
        """The message for the last finished close, whatever the hour: at
        8pm that's today's, at 11am or on a weekend it's the last trading
        day's. None if there's nothing to show."""
        if self._latest is None:
            return None
        quotes = self._latest(sorted(symbols), now)
        if not quotes:
            return None
        day = max(q.close_date for q in quotes)
        subs = [Sub(kind=market_repo.TICKER, league=league_for(s)) for s in symbols]
        digest = format_market_digest(quotes, subs, market_repo.MARKET_TZ, now, day=day)
        if digest.kind != "digest":
            return None
        return digest.text

    def _remove(self, did, symbols, now) -> str:
        if not symbols:
            return "Which symbol? For example: remove AAPL"
        lines = []
        removed = 0
        for symbol in dict.fromkeys(symbols):
            if market_repo.remove_ticker(self._db, did, symbol):
                lines.append("Removed %s." % symbol)
                removed += 1
            else:
                lines.append("You weren't following %s." % _echo(symbol))
        if removed:
            # For the operator's daily summary: how many, never which.
            repo.record_event(self._db, now, "ticker_remove", did, self._pepper,
                              detail={"count": removed})
        if not market_repo.tickers_for(self._db, did):
            lines.append("")
            lines.append("You're not following anything now, so no messages until you add one.")
        return "\n".join(lines)

    def _list(self, did) -> str:
        tickers = market_repo.tickers_for(self._db, did)
        if not tickers:
            return "You're not following anything yet. Try: add AAPL"
        return "You follow (%d of %d):\n%s" % (len(tickers), MAX_TICKERS, " ".join(tickers))


def _echo(word: str) -> str:
    """A word from the message, shortened before it goes into a reply."""
    return word if len(word) <= ECHO_CHARS else word[:ECHO_CHARS] + "\u2026"


def _fit(text: str) -> str:
    """Drop lines from the end until the reply fits in one Bluesky DM."""
    lines = text.split("\n")
    while len("\n".join(lines)) > MAX_MESSAGE and len(lines) > 1:
        lines.pop()
    return "\n".join(lines)[:MAX_MESSAGE]


def _kind(verb: str) -> str:
    """Which of our commands this is, or "unknown" to hand to the original
    handler. Names the command, not the word typed, so `rm` is "remove"."""
    if verb in ("config", "privacy", "admin"):
        return verb
    for name, words in (("add", ADD_WORDS), ("remove", REMOVE_WORDS),
                        ("list", LIST_WORDS), ("help", HELP_WORDS),
                        ("about", ABOUT_WORDS), ("test", TEST_WORDS),
                        ("quote", QUOTE_WORDS)):
        if verb in words:
            return name
    return "unknown"
