"""Scheduler process entry point.

Runs as its own systemd unit, never inside Flask: gunicorn runs multiple
workers and each would fire the same job. Spec section 3.

    python run_scheduler.py --once      one tick, then exit
    python run_scheduler.py             tick every minute

Build step 3 prints digests to stdout. Step 4 swaps in the chat sender.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from sportscrawl.config import require, settings
from sportscrawl.db import open_database
from sportscrawl.scheduler import Scheduler, stdout_sender
from sportscrawl.source import SOCCER_LEAGUES_AT_LAUNCH, US_PRO_LEAGUES, EspnSource
from sportscrawl.watchdog import Liveness

TICK_SECONDS = 60


def build(database_path: str, pepper: str, sender=None) -> Scheduler:
    return Scheduler(
        connection=open_database(database_path),
        source=EspnSource(),
        leagues=list(US_PRO_LEAGUES) + list(SOCCER_LEAGUES_AT_LAUNCH),
        pepper=pepper,
        sender=sender or stdout_sender,
    )


def chat_sender(config, allowed_dids):
    """Real delivery, restricted to an explicit allowlist.

    Step 4 is "chat send, to one DID only". Enforced in code rather than by
    remembering, because the failure mode is messaging strangers from a
    half-finished bot.
    """
    from sportscrawl.chat import ChatClient
    from sportscrawl.senders import AllowlistSender, ChatSender

    require(config, "BSKY_IDENTIFIER", "BSKY_APP_PASSWORD")
    client = ChatClient(config["BSKY_IDENTIFIER"], config["BSKY_APP_PASSWORD"])
    client.login()
    return AllowlistSender(ChatSender(client), allowed_dids)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sportscrawl scheduler")
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument(
        "--send",
        action="store_true",
        help="deliver by DM instead of stdout (requires --to)",
    )
    parser.add_argument(
        "--to",
        action="append",
        default=[],
        help="DID allowed to receive messages; repeatable",
    )
    parser.add_argument(
        "--database",
        default=os.environ.get("DATABASE_PATH", "sportscrawl.db"),
    )
    args = parser.parse_args(argv)

    config = settings()
    sender = None
    if args.send:
        if not args.to:
            print("--send requires at least one --to DID", file=sys.stderr)
            return 2
        sender = chat_sender(config, args.to)

    pepper = config.get("EVENTS_PEPPER") or os.environ.get("EVENTS_PEPPER")
    if not pepper:
        # Refuse rather than default. A changed pepper severs the join between
        # a signup and its later churn, so a silent fallback would quietly
        # corrupt the only metric the project exists to gather.
        print("EVENTS_PEPPER is not set", file=sys.stderr)
        return 2

    scheduler = build(args.database, pepper, sender=sender)

    live = Liveness("scheduler")
    if not args.once:
        live.start_watchdog()

    # Server timezone must be UTC and every conversion is explicit against the
    # subscriber's stored zone. Never rely on server local time. Spec section 3.
    while True:
        report = scheduler.tick(datetime.now(timezone.utc))
        if report.sent or report.failures or report.expired or report.errors:
            print("[%s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), report),
                  flush=True)
        live.tick()
        if args.once:
            return 0
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
