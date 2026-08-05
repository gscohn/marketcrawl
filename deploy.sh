#!/usr/bin/env bash
#
# Deploy sportscrawl. Run on the server as root:
#
#     /home/sportscrawl/app/deploy.sh
#
# Spec section 13. The order matters: migrations run against the old code
# still serving traffic, which is safe because migrations are additive and
# idempotent, then gunicorn restarts, then the two workers.
#
# Restarting mid-batch is safe. The per-message last_sent_date commit in
# section 7 is what makes a deploy at 07:05 resume rather than re-send.

set -euo pipefail

APP_DIR=/home/sportscrawl/app
SERVICE_USER=sportscrawl
ENV_FILE=/home/sportscrawl/.env
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31mFAILED: %s\033[0m\n' "$1" >&2; exit 1; }

as_app() { sudo -u "$SERVICE_USER" env SPORTSCRAWL_ENV_FILE="$ENV_FILE" "$@"; }

[ -f "$ENV_FILE" ] || fail "$ENV_FILE is missing"

# A CR anywhere in the env file ends up inside the value. That is not
# hypothetical: it once sent the whole team roster into a database file named
# "sportscrawl.db\r" while the app read an empty "sportscrawl.db" beside it,
# and both showed as the same name in ls.
if grep -q $'\r' "$ENV_FILE"; then
    fail "$ENV_FILE has CRLF line endings; every value carries a trailing CR"
fi

say "Pulling"
BEFORE_REQS=$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" rev-parse HEAD:requirements.txt 2>/dev/null || echo none)
sudo -u "$SERVICE_USER" git -C "$APP_DIR" pull --ff-only
AFTER_REQS=$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" rev-parse HEAD:requirements.txt 2>/dev/null || echo none)
sudo -u "$SERVICE_USER" git -C "$APP_DIR" log --oneline -1

if [ "$BEFORE_REQS" != "$AFTER_REQS" ]; then
    say "Dependencies changed, installing"
    as_app "$VENV/bin/pip" -q install -r "$APP_DIR/requirements.txt"
else
    echo "requirements.txt unchanged"
fi

say "Migrations"
# open_database applies pending numbered migrations and records them in
# schema_version. Every migration is safe to run twice.
as_app "$PY" - <<'PYTHON'
from sportscrawl.config import settings
from sportscrawl.db import connect, migrate

config = settings()
db = connect(config["DATABASE_PATH"])
applied = migrate(db)
print("applied: %s" % (applied or "nothing pending"))
PYTHON

say "Team data"
# Printing the count rather than signalling through an exit code: under
# `set -e` a non-zero exit ends the script before the branch can read it.
TEAMS=$(as_app "$PY" -c '
from sportscrawl.config import settings
from sportscrawl.db import open_database
print(open_database(settings()["DATABASE_PATH"]).execute(
    "SELECT COUNT(*) FROM teams").fetchone()[0])
')
if [ "$TEAMS" -eq 0 ]; then
    echo "no teams, seeding rosters and crests"
    (cd "$APP_DIR" && as_app "$PY" seed_teams.py)
else
    echo "teams already seeded: $TEAMS"
fi

say "Restarting"
# Gunicorn first so the config page is back quickly, then the two workers.
systemctl restart sportscrawl-web
systemctl restart sportscrawl-scheduler
systemctl restart sportscrawl-poller
sleep 4

say "Verifying"
# Checking that each step exited 0 is not enough. The seed script once printed
# "total 205" while writing to a file the app could not see, so this asks the
# running application what it can actually observe.

for unit in web scheduler poller; do
    state=$(systemctl is-active "sportscrawl-$unit" || true)
    printf '  %-28s %s\n' "sportscrawl-$unit" "$state"
    [ "$state" = "active" ] || fail "sportscrawl-$unit is $state"
done

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://sportscrawl.app/privacy)
printf '  %-28s HTTP %s\n' "https privacy page" "$code"
[ "$code" = "200" ] || fail "privacy page returned $code"

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://sportscrawl.app/static/vendor/bootstrap.min.css)
printf '  %-28s HTTP %s\n' "stylesheet" "$code"
[ "$code" = "200" ] || fail "stylesheet returned $code; an unstyled page shipped once"

# The application's own view of its data, through the same config loader the
# web process uses. This is the check that would have caught the phantom
# database.
as_app "$PY" - <<'PYTHON'
import sys
from sportscrawl.config import settings
from sportscrawl.db import open_database

config = settings()
db = open_database(config["DATABASE_PATH"])
teams = db.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
subs = db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
print("  %-28s %d" % ("teams visible to the app", teams))
print("  %-28s %d" % ("subscribers", subs))
print("  %-28s %s" % ("database", config["DATABASE_PATH"]))
if teams == 0:
    print("no teams visible: the config page will render an empty picker", file=sys.stderr)
    raise SystemExit(1)
PYTHON

# ACME must not be proxied to gunicorn or cert renewal breaks silently, and
# .app is HSTS preloaded so an expired cert is an unreachable site.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
    http://sportscrawl.app/.well-known/acme-challenge/deploy-probe || echo 000)
printf '  %-28s HTTP %s (404 from Apache is correct)\n' "acme path not proxied" "$code"

say "Deployed"
