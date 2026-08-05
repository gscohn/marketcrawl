"""Issue a config or admin link from the command line.

For support and debugging, per spec section 11: same capability as the bot's
`config` command, different friction, and the web UI stays aggregate-only.

    python issue_token.py did:plc:...
    python issue_token.py did:plc:... --scope admin

Exists because minting tokens by hand over SSH is quoting-fragile and fails
silently. It failed exactly that way once, producing a link that had never
been written to the database.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timedelta, timezone

from sportscrawl import repo
from sportscrawl.config import settings
from sportscrawl.db import open_database

TTL = timedelta(minutes=15)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Issue a settings link")
    parser.add_argument("did")
    parser.add_argument("--scope", default="config", choices=("config", "admin"))
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--database", default=None)
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args(argv)

    config = settings()
    database = args.database or config.get("DATABASE_PATH", "sportscrawl.db")
    base = (args.base_url or config.get("BASE_URL") or "https://sportscrawl.app").rstrip("/")

    db = open_database(database)
    if repo.get_subscriber(db, args.did) is None:
        # A token whose DID has no subscriber row redeems into a session that
        # cannot load anything. Fail loudly rather than hand over a dead link.
        print("no subscriber with did %s" % args.did, file=sys.stderr)
        return 2

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    repo.issue_token(db, args.did, token, args.scope, now + timedelta(minutes=args.minutes))

    # Read it back. The whole reason this script exists is that a write which
    # silently did not happen produced a link that looked fine.
    stored = db.execute(
        "SELECT token FROM config_tokens WHERE token = ?", (token,)
    ).fetchone()
    if stored is None:
        print("token was not persisted", file=sys.stderr)
        return 1

    print("%s/c/%s" % (base, token))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
