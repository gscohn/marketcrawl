"""Run the market-close scheduler.

ELI5: this is the "alarm clock" process. Every minute it wakes up, checks
whether anyone's message is due (a few minutes after the stock market
closes), fetches prices if so, and sends. By default "sends" means "prints to
this terminal", so you can watch it work without a Bluesky account.

    python run_market.py --once      check once, then exit
    python run_market.py             check every minute until stopped

    python run_market.py --once --send --only did:plc:abc
                                     send real Bluesky DMs, to one person only
    python run_market.py --once --at 2026-10-30T20:20:00Z
                                     pretend it's that moment (for testing)

The daily public posts (prices, and on earnings days a second one about big
companies' results; see market_feed.py) are switched on with
`python feed.py enable`. They're a DRY RUN (printed, not posted) unless
--post is given, which publishes them on the bot's Bluesky profile.

If ADMIN_DID is set, that person gets a short summary DM 90 minutes after
each close (posts sent, DMs sent, people joining and leaving; see
market_report.py), and an alert if sending keeps failing.

Filings from the SEC are included when SEC_CONTACT is set (the SEC requires
an email address in every request; see edgar_source.py).

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

from sportscrawl.chat import ChatClient
from sportscrawl.config import load_env_file, require, settings
from sportscrawl.db import open_database
from sportscrawl.edgar_source import EdgarSource
from sportscrawl.logs import configure
from sportscrawl.market_feed import FeedRouter, classify_for, dry_run_poster, format_for
from sportscrawl.market_post import BlueskyPoster, PostClient
from sportscrawl.market_repo import ticker_leagues
from sportscrawl.market_report import DailySummary
from sportscrawl.market_source import MarketSource, YahooSource
from sportscrawl.scheduler import Scheduler, stdout_sender
from sportscrawl.senders import AllowlistSender, ChatSender

TICK_SECONDS = 60
DEFAULT_DATABASE = "marketcrawl.db"


def sec_contact() -> str | None:
    """Read outside settings(): the original's settings only pass through
    the keys it knows, and this one is ours."""
    return os.environ.get("SEC_CONTACT") or load_env_file().get("SEC_CONTACT")


def default_source() -> MarketSource:
    contact = sec_contact()
    return MarketSource(YahooSource(), EdgarSource(contact) if contact else None,
                        movers=True)


def build(database_path: str, pepper: str, sender=None, source=None,
          poster=dry_run_poster, admin_did=None) -> Scheduler:
    return Scheduler(
        connection=open_database(database_path),
        source=source or default_source(),
        # A function, not a list: which symbols to fetch depends on what
        # people follow right now, so it's looked up fresh every tick.
        leagues=ticker_leagues,
        pepper=pepper,
        # The public feed's post goes to the poster (printed, unless
        # --post); everyone else's message goes to `sender`.
        sender=FeedRouter(sender or stdout_sender, poster),
        formatter=format_for,
        # The market clock, with the earnings post on its later one.
        classifier=classify_for,
        # The original's "sending keeps failing" alert, to the operator.
        alert_did=admin_did,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Marketcrawl scheduler")
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument("--database",
                        default=os.environ.get("DATABASE_PATH", DEFAULT_DATABASE))
    parser.add_argument("--send", action="store_true",
                        help="send real Bluesky DMs instead of printing")
    parser.add_argument("--post", action="store_true",
                        help="publish the daily public post instead of printing it")
    parser.add_argument("--only", action="append", metavar="DID",
                        help="with --send: refuse to message anyone else (repeatable)")
    parser.add_argument("--at", metavar="ISO_TIME",
                        help="with --once: pretend it's this moment, e.g. 2026-10-30T20:20:00Z")
    args = parser.parse_args(argv)
    if args.at and not args.once:
        parser.error("--at only makes sense with --once")

    configure()
    config = settings()
    pepper = config.get("EVENTS_PEPPER") or os.environ.get("EVENTS_PEPPER")
    if not pepper:
        # Same refusal as the original: the pepper scrambles IDs in the
        # stats table, and a silent default would quietly scramble them
        # differently on every machine.
        print("EVENTS_PEPPER is not set (put it in .env)", file=sys.stderr)
        return 2

    sender = None
    if args.send:
        require(config, "BSKY_IDENTIFIER", "BSKY_APP_PASSWORD")
        client = ChatClient(config["BSKY_IDENTIFIER"], config["BSKY_APP_PASSWORD"])
        client.login()
        sender = ChatSender(client)
        if args.only:
            # The original's safety catch for testing: a half-finished bot
            # can only message the people named here.
            sender = AllowlistSender(sender, args.only)

    poster = dry_run_poster
    if args.post:
        require(config, "BSKY_IDENTIFIER", "BSKY_APP_PASSWORD")
        poster = BlueskyPoster(PostClient(config["BSKY_IDENTIFIER"], config["BSKY_APP_PASSWORD"]))

    admin_did = config.get("ADMIN_DID")
    scheduler = build(args.database, pepper, sender=sender, poster=poster, admin_did=admin_did)
    # The operator's daily summary. It shares the scheduler's database and
    # DM sender, so without --send it's printed like everything else.
    summary = DailySummary(open_database(args.database), pepper, admin_did,
                           sender or stdout_sender, dry_run=not args.post,
                           filings=bool(sec_contact()))
    while True:
        now = (datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at
               else datetime.now(timezone.utc))
        report = scheduler.tick(now)
        summary.count_errors(now, report.errors)
        if summary.maybe_send(now):
            print("operator summary sent", flush=True)
        print("[%s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), report),
              flush=True)
        if args.once:
            return 0
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
