"""Run the market bot's DM inbox.

ELI5: this is the "front desk" process. Every 20 seconds it checks the bot's
Bluesky DMs, works out what each new message asks for (`add AAPL`, `list`,
`help`...), does it, and replies.

    python run_market_poller.py --once     check the inbox once, then exit
    python run_market_poller.py            keep checking until stopped

It's the original run_poller.py with one part swapped: the market command
handler (market_commands.py) answers instead of the sports one. The Bluesky
login, the inbox reading and the replying are the original's, unchanged.

Needs BSKY_IDENTIFIER and BSKY_APP_PASSWORD (the bot account's handle and an
app password with DM access) and EVENTS_PEPPER, from the environment or .env.
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

from sportscrawl.chat import ChatClient
from sportscrawl.config import require, settings
from sportscrawl.db import open_database
from sportscrawl.logs import configure
from sportscrawl.market_commands import MarketCommandHandler
from sportscrawl.market_source import YahooSource
from sportscrawl.poller import Poller
from sportscrawl.watchdog import Liveness

from run_market import DEFAULT_DATABASE

POLL_SECONDS = 20


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Marketcrawl DM poller")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--database",
                        default=os.environ.get("DATABASE_PATH", DEFAULT_DATABASE))
    args = parser.parse_args(argv)

    configure()
    config = settings()
    require(config, "BSKY_IDENTIFIER", "BSKY_APP_PASSWORD", "EVENTS_PEPPER")

    db = open_database(args.database)
    client = ChatClient(config["BSKY_IDENTIFIER"], config["BSKY_APP_PASSWORD"])
    bot_did = client.login()
    # `add` checks each symbol exists by asking Yahoo, and a newcomer's
    # first add replies with the latest close, so the handler gets both.
    # One source for both, so the lookup's download is reused for the report.
    prices = YahooSource()
    handler = MarketCommandHandler(db, config["EVENTS_PEPPER"],
                                   lookup=prices.lookup, latest=prices.latest)
    poller = Poller(db, client, handler, bot_did=bot_did)

    live = Liveness("poller")
    if not args.once:
        live.start_watchdog()

    while True:
        report = poller.poll(datetime.now(timezone.utc))
        if report.handled:
            print("[%s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), report),
                  flush=True)
        live.tick()
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
