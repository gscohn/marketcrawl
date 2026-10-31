#!/usr/bin/env bash
#
# Update the server to the latest code on GitHub. Run as root on the server:
#
#     /home/marketcrawl/app/deploy/market/deploy_market.sh
#
# or from your own computer:
#
#     ssh root@<server> /home/marketcrawl/app/deploy/market/deploy_market.sh
#
# ELI5: download the new code, install anything new it needs, update the
# database layout if the new code changed it, restart both bot services,
# and check they came back up. Restarting mid-send is safe: the bot writes
# down each message as it sends it, so it carries on rather than repeating.

set -euo pipefail

APP_DIR=/home/marketcrawl/app
PY=$APP_DIR/.venv/bin/python

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
as_app() { sudo -u marketcrawl "$@"; }

# Pull, then restart this script from the freshly pulled copy (bash reads a
# script as it goes, so a pull that changes this file mid-run would confuse
# it; the original deploy.sh learned this the hard way).
if [ "${DEPLOY_REEXEC:-}" != "1" ]; then
    say "Pulling"
    BEFORE=$(as_app git -C "$APP_DIR" rev-parse HEAD:requirements.txt)
    as_app git -C "$APP_DIR" pull --ff-only
    as_app git -C "$APP_DIR" log --oneline -1
    AFTER=$(as_app git -C "$APP_DIR" rev-parse HEAD:requirements.txt)
    export DEPLOY_REEXEC=1 BEFORE AFTER
    exec bash "$APP_DIR/deploy/market/deploy_market.sh" "$@"
fi

if [ "$BEFORE" != "$AFTER" ]; then
    say "Dependencies changed, installing"
    as_app "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi

say "Services"
# Pick up any changes to the service files themselves.
for unit in marketcrawl-scheduler.service marketcrawl-poller.service \
            marketcrawl-backup.service marketcrawl-backup.timer; do
    cp "$APP_DIR/deploy/market/$unit" /etc/systemd/system/
done
chmod +x "$APP_DIR/deploy/market/"*.sh
systemctl daemon-reload

say "Database"
# Opening the database applies any new migrations (each is safe to rerun).
(cd "$APP_DIR" && as_app "$PY" -c "
from sportscrawl.db import open_database
db = open_database('/home/marketcrawl/marketcrawl.db')
print('  subscribers', db.execute('SELECT COUNT(*) FROM subscribers').fetchone()[0])
")

say "Restarting"
systemctl restart marketcrawl-poller marketcrawl-scheduler
sleep 5
for unit in poller scheduler; do
    state=$(systemctl is-active "marketcrawl-$unit" || true)
    printf '  %-24s %s\n' "marketcrawl-$unit" "$state"
    [ "$state" = "active" ] || { journalctl -u "marketcrawl-$unit" -n 20 --no-pager; exit 1; }
done

say "Deployed"
