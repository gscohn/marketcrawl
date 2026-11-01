"""Switch the daily public post on or off, or preview it.

ELI5: the public post is sent by the same alarm-clock process as the DMs
(run_market.py), to a pretend subscriber called the "feed". This script
adds or removes that pretend subscriber, and can show you what today's post
would say without sending anything.

    python feed.py preview     print the post for the latest close
    python feed.py enable      run_market.py will write it every trading day
    python feed.py disable     stop

It is a DRY RUN (printed, not posted) unless run_market.py is given --post.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from run_market import DEFAULT_DATABASE
from sportscrawl import market_feed
from sportscrawl.config import settings
from sportscrawl.db import open_database
from sportscrawl.market_source import YahooSource


def preview(now) -> int:
    prices = YahooSource()
    quotes = prices.latest([symbol for symbol, _ in market_feed.FEED_INDEXES], now)
    if not quotes:
        print("No index prices available right now.", file=sys.stderr)
        return 1
    day = max(q.close_date for q in quotes)
    digest = market_feed.format_feed_post(quotes + prices.movers(now), day)
    if digest.text is None:
        print("Nothing to post.", file=sys.stderr)
        return 1
    print(digest.text)
    print("\n(%d of %d characters)" % (len(digest.text), market_feed.POST_LIMIT))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Marketcrawl public post")
    parser.add_argument("action", choices=("preview", "enable", "disable"))
    parser.add_argument("--database",
                        default=os.environ.get("DATABASE_PATH", DEFAULT_DATABASE))
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.action == "preview":
        return preview(now)

    pepper = settings().get("EVENTS_PEPPER") or os.environ.get("EVENTS_PEPPER")
    if not pepper:
        print("EVENTS_PEPPER is not set (put it in .env)", file=sys.stderr)
        return 2
    db = open_database(args.database)
    if args.action == "enable":
        market_feed.enable(db, now, pepper)
        print("Public post on (dry run). run_market.py will write it after each close.")
    else:
        market_feed.disable(db, now, pepper)
        print("Public post off.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
