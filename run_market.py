"""Run the market-close scheduler.

ELI5: this is the "alarm clock" process. Every minute it wakes up, checks
whether anyone's message is due (a few minutes after the stock market
closes), fetches prices if so, and sends. By default "sends" means "prints to
this terminal", so you can watch it work without a Bluesky account.

    python run_market.py --once      check once, then exit
    python run_market.py             check every minute until stopped

It's the original run_scheduler.py with the market pieces plugged in: the
Yahoo source instead of ESPN, the market clock instead of each person's
chosen send time, and the market formatter instead of the sports one.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from sportscrawl.config import settings
from sportscrawl.db import open_database
from sportscrawl.logs import configure
from sportscrawl.market_formatter import format_market_digest
from sportscrawl.market_repo import ticker_leagues
from sportscrawl.market_schedule import classify_market
from sportscrawl.market_source import YahooSource
from sportscrawl.scheduler import Scheduler, stdout_sender

TICK_SECONDS = 60
DEFAULT_DATABASE = "marketcrawl.db"


def build(database_path: str, pepper: str, sender=None, source=None) -> Scheduler:
    return Scheduler(
        connection=open_database(database_path),
        source=source or YahooSource(),
        # A function, not a list: which symbols to fetch depends on what
        # people follow right now, so it's looked up fresh every tick.
        leagues=ticker_leagues,
        pepper=pepper,
        sender=sender or stdout_sender,
        formatter=format_market_digest,
        classifier=classify_market,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Marketcrawl scheduler")
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument("--database",
                        default=os.environ.get("DATABASE_PATH", DEFAULT_DATABASE))
    args = parser.parse_args(argv)

    configure()
    pepper = settings().get("EVENTS_PEPPER") or os.environ.get("EVENTS_PEPPER")
    if not pepper:
        # Same refusal as the original: the pepper scrambles IDs in the
        # stats table, and a silent default would quietly scramble them
        # differently on every machine.
        print("EVENTS_PEPPER is not set (put it in .env)", file=sys.stderr)
        return 2

    scheduler = build(args.database, pepper)
    while True:
        report = scheduler.tick(datetime.now(timezone.utc))
        print("[%s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), report),
              flush=True)
        if args.once:
            return 0
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
