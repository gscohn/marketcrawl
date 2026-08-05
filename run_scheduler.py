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

from sportscrawl.db import open_database
from sportscrawl.scheduler import Scheduler, stdout_sender
from sportscrawl.source import SOCCER_LEAGUES_AT_LAUNCH, US_PRO_LEAGUES, EspnSource

TICK_SECONDS = 60


def build(database_path: str, pepper: str) -> Scheduler:
    return Scheduler(
        connection=open_database(database_path),
        source=EspnSource(),
        leagues=list(US_PRO_LEAGUES) + list(SOCCER_LEAGUES_AT_LAUNCH),
        pepper=pepper,
        sender=stdout_sender,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Sportscrawl scheduler")
    parser.add_argument("--once", action="store_true", help="one tick, then exit")
    parser.add_argument(
        "--database",
        default=os.environ.get("DATABASE_PATH", "sportscrawl.db"),
    )
    args = parser.parse_args(argv)

    pepper = os.environ.get("EVENTS_PEPPER")
    if not pepper:
        # Refuse rather than default. A changed pepper severs the join between
        # a signup and its later churn, so a silent fallback would quietly
        # corrupt the only metric the project exists to gather.
        print("EVENTS_PEPPER is not set", file=sys.stderr)
        return 2

    scheduler = build(args.database, pepper)

    # Server timezone must be UTC and every conversion is explicit against the
    # subscriber's stored zone. Never rely on server local time. Spec section 3.
    while True:
        report = scheduler.tick(datetime.now(timezone.utc))
        if report.sent or report.failures or report.expired:
            print("[%s] %s" % (datetime.now(timezone.utc).strftime("%H:%M:%S"), report))
        if args.once:
            return 0
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
