"""Daily operations summary, sent to the operator by DM.

Without this the first sign of trouble is a subscriber mentioning it. The
watchdog restarts a stalled loop, but nothing tells anyone that ESPN went
away, that sends are failing, or that the backup stopped running.

Deliberately short and boring. A report nobody reads is not monitoring, so it
fits in a glance and only says something is wrong when something is.

    python daily_report.py            send it
    python daily_report.py --dry-run  print it
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone

from sportscrawl import repo, stats
from sportscrawl.chat import ChatClient
from sportscrawl.config import require, settings
from sportscrawl.db import open_database
from sportscrawl.logs import configure
from sportscrawl.senders import ChatSender

BACKUP_DIR = "/home/sportscrawl/backups"
BACKUP_STALE_HOURS = 36


def _since(db, event_type, hours=24):
    cutoff = repo.utc_string(datetime.now(timezone.utc) - timedelta(hours=hours))
    return db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ? AND ts >= ?",
        (event_type, cutoff),
    ).fetchone()[0]


def backup_age_hours(directory=BACKUP_DIR):
    """None when there are no snapshots at all, which is itself the alarm."""
    try:
        files = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith(".db.gz")
        ]
    except OSError:
        return None
    if not files:
        return None
    newest = max(os.path.getmtime(path) for path in files)
    return (datetime.now(timezone.utc).timestamp() - newest) / 3600.0


def build(db, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    state = stats.state_counts(db)

    lines = ["SportsCrawl daily", ""]
    lines.append(
        "Subscribers: %d active, %d paused" % (state["active"], state["paused"])
    )

    sent = _since(db, "digest_sent")
    failures = _since(db, "failure_notice")
    lines.append("Last 24h: %d digests, %d failure notices" % (sent, failures))

    signups = _since(db, "signup")
    deletes = _since(db, "delete")
    if signups or deletes:
        lines.append("Joined %d, left %d" % (signups, deletes))

    # Only mention leagues when something is actually wrong. A healthy list
    # every morning trains you to skim past the one line that matters.
    unhealthy = [
        row for row in stats.league_health(db) if row["consecutive_failures"]
    ]
    if unhealthy:
        lines.append("")
        lines.append("Data source problems:")
        for row in unhealthy[:6]:
            lines.append(
                "  %s: %d consecutive failures"
                % (row["league"], row["consecutive_failures"])
            )
    else:
        lines.append("Data sources: all healthy")

    age = backup_age_hours()
    if age is None:
        lines.append("Backup: NONE FOUND")
    elif age > BACKUP_STALE_HOURS:
        lines.append("Backup: STALE, %.0f hours old" % age)
    else:
        lines.append("Backup: %.0f hours old" % age)

    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Daily operations summary")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--database", default=None)
    args = parser.parse_args(argv)

    configure()
    config = settings()
    database = args.database or config.get("DATABASE_PATH", "sportscrawl.db")
    db = open_database(database)

    text = build(db)

    if args.dry_run:
        print(text)
        return 0

    admin_did = config.get("ADMIN_DID")
    if not admin_did:
        print("ADMIN_DID is not set; nothing to report to")
        return 2

    require(config, "BSKY_IDENTIFIER", "BSKY_APP_PASSWORD")
    client = ChatClient(config["BSKY_IDENTIFIER"], config["BSKY_APP_PASSWORD"])
    client.login()
    ChatSender(client)(admin_did, text)
    print("report sent to %s" % admin_did)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
