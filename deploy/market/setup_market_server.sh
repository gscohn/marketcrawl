#!/usr/bin/env bash
#
# Set up a fresh Ubuntu 24.04 server to run marketcrawl. Run once, as root:
#
#     ssh root@<server> 'bash -s' < deploy/market/setup_market_server.sh
#
# ELI5, what it does:
#   1. installs Python, git and a firewall, and turns on automatic security
#      updates
#   2. makes a `marketcrawl` user that owns and runs the bot (not root)
#   3. downloads the code from GitHub and installs its one dependency
#   4. creates an empty secrets file (/home/marketcrawl/.env) for you to fill
#   5. installs the two bot services and the daily backup, but does NOT start
#      the bot: it can't log in to Bluesky until the secrets are filled in
#   6. allows only SSH in, and only with keys (no passwords)
#
# Safe to run again: each step checks whether it's already done.

set -euo pipefail

REPO=https://github.com/gscohn/marketcrawl.git
HOME_DIR=/home/marketcrawl
APP_DIR=$HOME_DIR/app
ENV_FILE=$HOME_DIR/.env

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3-venv git sqlite3 ufw unattended-upgrades
# Install security fixes automatically, every day.
dpkg-reconfigure -f noninteractive unattended-upgrades

say "User"
id marketcrawl >/dev/null 2>&1 || useradd --create-home --shell /bin/bash marketcrawl
chmod 750 "$HOME_DIR"

say "Code"
if [ -d "$APP_DIR/.git" ]; then
    sudo -u marketcrawl git -C "$APP_DIR" pull --ff-only
else
    sudo -u marketcrawl git clone -q "$REPO" "$APP_DIR"
fi
sudo -u marketcrawl git -C "$APP_DIR" log --oneline -1
[ -x "$APP_DIR/.venv/bin/python" ] || sudo -u marketcrawl python3 -m venv "$APP_DIR/.venv"
sudo -u marketcrawl "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "Secrets file"
# Created empty, readable only by the marketcrawl user. Never overwritten.
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<'ENV'
# marketcrawl secrets. Fill in, one per line, no quotes, no spaces around =.
BSKY_IDENTIFIER=
BSKY_APP_PASSWORD=
EVENTS_PEPPER=
# Optional: an email address the SEC can contact; turns on the filings section.
SEC_CONTACT=
ENV
fi
chown marketcrawl:marketcrawl "$ENV_FILE"
chmod 600 "$ENV_FILE"

say "Services"
chmod +x "$APP_DIR/deploy/market/"*.sh
for unit in marketcrawl-scheduler.service marketcrawl-poller.service \
            marketcrawl-backup.service marketcrawl-backup.timer; do
    cp "$APP_DIR/deploy/market/$unit" /etc/systemd/system/
done
systemctl daemon-reload
# Start at every boot from now on, but not right now (secrets first).
systemctl enable marketcrawl-scheduler marketcrawl-poller
systemctl enable --now marketcrawl-backup.timer

say "Firewall and SSH"
# The bot only makes outgoing connections, so the only way in is SSH.
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
ufw status | head -4
# Keys only: no password logins, for anyone.
cat > /etc/ssh/sshd_config.d/10-keys-only.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
SSHD
sshd -t && systemctl reload ssh

say "Done"
echo "Next: fill in $ENV_FILE, then start the bot:"
echo "    systemctl start marketcrawl-poller marketcrawl-scheduler"
