"""Replay the last couple of weeks of market closes, in a few seconds.

ELI5: to test "does the right message go out after each close?" you'd
normally have to wait a day per test. But Yahoo keeps history, so we can ask
"what would the bot have sent at 4:15pm last Tuesday?" for every day in the
last two weeks, one after another, right now. It's the market version of the
original replay.py.

This drives the real scheduler, the real market clock and the real
formatter. The only thing it fakes is the time: it sets the clock to 15
minutes after each day's close. Weekends and holidays show as closed.

    python replay_market.py
    python replay_market.py --days 30 --ticker AAPL --ticker ^GSPC

It uses a throwaway database in the system temp folder, never your real one.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from datetime import datetime, timedelta, timezone

from run_market import build
from sportscrawl import market_repo, repo
from sportscrawl.market_commands import clean_symbol
from sportscrawl.market_schedule import CLOSE_DELAY
from sportscrawl.market_source import YahooSource
from sportscrawl.nyse_calendar import NEW_YORK, new_york_date, session
from sportscrawl.scheduler import FAILURE_GRACE

DID = "did:plc:replay"
PEPPER = "replay-pepper"


class Printer:
    """Stands in for Bluesky: prints each message and counts them."""

    def __init__(self):
        self.sent = 0

    def __call__(self, did, text):
        self.sent += 1
        print(text)
        print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay recent market closes")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--ticker", action="append", default=[],
                        help="symbol to follow; repeatable (default: ^GSPC AAPL)")
    args = parser.parse_args(argv)
    # Same clean-up as the `add` command, so `sp500` means ^GSPC here too.
    tickers = [clean_symbol(t) for t in (args.ticker or ["^GSPC", "AAPL"])]

    path = os.path.join(tempfile.mkdtemp(prefix="mcreplay"), "replay.db")
    printer = Printer()
    # Three months of history so every replayed day has a "day before" to
    # compare with. A long cache life, because the pretend clock jumps a day
    # at a time and we don't want to re-download for every jump.
    source = YahooSource(ttl=timedelta(days=365), range_="3mo")
    scheduler = build(path, PEPPER, sender=printer, source=source)
    db = scheduler._db

    now = datetime.now(timezone.utc)
    repo.create_subscriber(db, DID, now, PEPPER)
    for symbol in tickers:
        market_repo.add_ticker(db, DID, symbol)
    market_repo.ensure_market_tz(db, DID)

    today = new_york_date(now)
    sessions = 0
    for offset in range(args.days, -1, -1):
        day = today - timedelta(days=offset)
        hours = session(day)
        label = day.strftime("%a %Y-%m-%d")
        if hours is None:
            print("== %s  market closed" % label)
            continue
        moment = hours[1] + CLOSE_DELAY
        if moment > now:
            print("== %s  hasn't closed yet" % label)
            continue

        sessions += 1
        print("== %s  at %s ET" % (label, moment.astimezone(NEW_YORK).strftime("%H:%M")))
        # Each pretend day starts fresh: forget that we "already sent today".
        db.execute("UPDATE subscribers SET last_evaluated_date = NULL,"
                   " last_sent_date = NULL, last_failure_date = NULL WHERE did = ?",
                   (DID,))
        report = scheduler.tick(moment)
        if report.deferred:
            # A price wasn't ready. The live scheduler would keep trying
            # every minute and, after the grace period, send what it has
            # with a "some prices are missing" line. Jump straight there.
            report = scheduler.tick(moment + FAILURE_GRACE)
        if not (report.sent or report.failures):
            print("(nothing sent: %s)\n" % report)

    print("%d messages for %d trading sessions" % (printer.sent, sessions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
