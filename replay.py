"""Replay past mornings against real ESPN data.

Build step 4 says run for a week before trusting it. A week of waiting is not
the same thing as a week of coverage: ESPN serves historical scoreboards, so
the same seven mornings can be replayed in seconds against the data those
mornings actually had.

This drives the real scheduler, not a mock. Same classify(), same commit
table, same formatter, so what it shows is what would have been delivered.

    python replay.py --days 7
    python replay.py --days 7 --send --to did:plc:...

A replay database is created under the system temp directory by default so a
dry run never touches production state.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sportscrawl import repo
from sportscrawl.config import settings
from sportscrawl.db import open_database
from sportscrawl.scheduler import Scheduler
from sportscrawl.source import (
    SOCCER_LEAGUES_AT_LAUNCH,
    US_PRO_LEAGUES,
    EspnSource,
    WindowResult,
)

DEFAULT_SUBS = [
    ("team", "baseball/mlb", "19"),        # Dodgers
    ("team", "basketball/wnba", "9"),      # Storm
    ("competition", "soccer/usa.1", None),  # the MLS slate
]


def as_of(events, moment):
    """Reconstruct what ESPN would have reported at `moment`.

    Historical scoreboards report every game as final, including games that,
    at the simulated hour, had not been played yet. Left alone, the replay
    files tonight's games under Final at breakfast and then shows them again
    the next morning as yesterday's results.

    A game that starts after `moment` had not started: no state, no score, no
    winner. This is the only thing the replay fakes, and it exists to stop the
    replay lying in the optimistic direction.
    """
    rebuilt = []
    for event in events:
        if event.start > moment:
            rebuilt.append(
                replace(event, state="pre", home_score=None, away_score=None, winner=None)
            )
        else:
            rebuilt.append(event)
    return rebuilt


class AsOfSource:
    """Wraps the real adapter so the scheduler sees a truthful past."""

    def __init__(self, inner):
        self._inner = inner

    @property
    def health(self):
        return self._inner.health

    def fetch_window(self, leagues, now):
        result = self._inner.fetch_window(leagues, now)
        return WindowResult(
            events=as_of(result.events, now),
            failed_leagues=result.failed_leagues,
            degraded_leagues=result.degraded_leagues,
        )


class Tee:
    """Prints every digest, and optionally also delivers it."""

    def __init__(self, inner=None):
        self.inner = inner
        self.digests = []

    def __call__(self, did, text):
        self.digests.append((did, text))
        print("-" * 60)
        print(text)
        print()
        if self.inner is not None:
            self.inner(did, text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Replay past mornings")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--tz", default="America/Los_Angeles")
    parser.add_argument("--send-time", default="07:00")
    parser.add_argument("--did", default="did:plc:replay")
    parser.add_argument("--send", action="store_true", help="also deliver by DM")
    parser.add_argument("--to", action="append", default=[])
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    config = settings()
    pepper = config.get("EVENTS_PEPPER", "replay-pepper")

    path = args.database or os.path.join(tempfile.mkdtemp(prefix="screplay"), "replay.db")
    db = open_database(path)

    zone = ZoneInfo(args.tz)
    now = datetime.now(timezone.utc)
    repo.create_subscriber(db, args.did, now, pepper, tz=args.tz, send_time=args.send_time)
    for kind, league, team_id in DEFAULT_SUBS:
        repo.add_sub(db, args.did, kind, league, team_id)

    delivery = None
    if args.send:
        if not args.to:
            print("--send requires --to", file=sys.stderr)
            return 2
        from run_scheduler import chat_sender

        delivery = chat_sender(config, args.to)

    sender = Tee(delivery)
    scheduler = Scheduler(
        db,
        AsOfSource(EspnSource()),
        list(US_PRO_LEAGUES) + list(SOCCER_LEAGUES_AT_LAUNCH),
        pepper,
        sender=sender,
        sleep=lambda _: None,
        gap=0,
    )

    hour, minute = (int(part) for part in args.send_time.split(":"))
    today_local = now.astimezone(zone).date()

    for offset in range(args.days, 0, -1):
        day = today_local - timedelta(days=offset)
        moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        print("=" * 60)
        print("%s  %s %s" % (day.strftime("%a %Y-%m-%d"), args.send_time, args.tz))

        # Each simulated morning is a fresh decision. This and as_of() above
        # are the only two things replay fakes; the scheduler, the commit
        # table, and the formatter are all the real path.
        db.execute(
            "UPDATE subscribers SET last_evaluated_date = NULL,"
            " last_sent_date = NULL, last_failure_date = NULL WHERE did = ?",
            (args.did,),
        )
        report = scheduler.tick(moment.astimezone(timezone.utc))
        if not (report.sent or report.failures):
            print("(silent: no games)\n")

    print("=" * 60)
    print("%d of %d mornings produced a digest" % (len(sender.digests), args.days))
    print("database: %s" % path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
