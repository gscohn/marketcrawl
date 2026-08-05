"""Inbound DM poll loop entry point.

    python run_poller.py --once
    python run_poller.py              poll every 20 seconds

Runs alongside the scheduler. Both are separate from Flask: gunicorn runs
multiple workers and each would fire the same job. Spec section 3.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from sportscrawl.chat import ChatClient
from sportscrawl.commands import CommandHandler
from sportscrawl.config import require, settings
from sportscrawl.db import open_database
from sportscrawl.poller import Poller
from sportscrawl.watchdog import Liveness

POLL_SECONDS = 20


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sportscrawl DM poller")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    config = settings()
    require(config, "BSKY_IDENTIFIER", "BSKY_APP_PASSWORD", "EVENTS_PEPPER")

    database = (
        args.database
        or config.get("DATABASE_PATH")
        or os.environ.get("DATABASE_PATH", "sportscrawl.db")
    )
    base_url = args.base_url or config.get("BASE_URL") or "https://sportscrawl.app"

    db = open_database(database)
    client = ChatClient(config["BSKY_IDENTIFIER"], config["BSKY_APP_PASSWORD"])
    bot_did = client.login()
    handler = CommandHandler(db, config["EVENTS_PEPPER"], base_url,
                             admin_did=config.get("ADMIN_DID"))
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
