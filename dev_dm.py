"""Pretend to DM the bot, from the terminal.

ELI5: instead of opening Bluesky and messaging the bot, type the message
here. It goes through exactly the same command code the real bot uses, writes
to the same local database run_market.py reads, and prints the reply.

    python dev_dm.py "add AAPL sp500"
    python dev_dm.py "list"
    python dev_dm.py --did did:plc:someone-else "help"
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from run_market import DEFAULT_DATABASE
from sportscrawl.chat import Message
from sportscrawl.config import settings
from sportscrawl.db import open_database
from sportscrawl.market_commands import MarketCommandHandler
from sportscrawl.market_source import YahooSource


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Send the bot a pretend DM")
    parser.add_argument("text", help="what you'd type in the DM")
    parser.add_argument("--did", default="did:plc:dev-subscriber",
                        help="who the message is from (a made-up ID is fine)")
    parser.add_argument("--database",
                        default=os.environ.get("DATABASE_PATH", DEFAULT_DATABASE))
    args = parser.parse_args(argv)

    pepper = settings().get("EVENTS_PEPPER") or os.environ.get("EVENTS_PEPPER")
    if not pepper:
        print("EVENTS_PEPPER is not set (put it in .env)", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    prices = YahooSource()
    handler = MarketCommandHandler(open_database(args.database), pepper,
                                   lookup=prices.lookup, latest=prices.latest,
                                   live=prices.live)
    message = Message(message_id="dev", convo_id="dev-" + args.did,
                      sender_did=args.did, text=args.text, sent_at="")
    print(handler.handle(message, now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
