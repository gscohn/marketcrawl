"""Environment configuration.

Secrets live in an env file outside the repo, mode 600, referenced from the
systemd units with EnvironmentFile=. Locally a .env sits alongside the code
and is gitignored. Spec section 13.
"""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_ENV_FILE = ".env"


def env_file_path(path=None) -> str:
    """Where the env file lives.

    Production points SPORTSCRAWL_ENV_FILE at /home/sportscrawl/.env, outside
    the repo working tree. A copy inside the tree would be one .gitignore edit
    away from being committed, which for a full-access credential is not a
    risk worth carrying for convenience.
    """
    return path or os.environ.get("SPORTSCRAWL_ENV_FILE", DEFAULT_ENV_FILE)


def load_env_file(path=None) -> dict:
    """Parse a KEY=value file. Absent file is not an error."""
    values = {}
    location = Path(env_file_path(path))
    if not location.exists():
        return values

    for line in location.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


REQUIRED = ("BSKY_IDENTIFIER", "BSKY_APP_PASSWORD", "EVENTS_PEPPER")
OPTIONAL = (
    "DATABASE_PATH",
    "ADMIN_DID",
    "ADMIN_PASSWORD",
    "FLASK_SECRET_KEY",
    "BASE_URL",
)
KNOWN = REQUIRED + OPTIONAL


def settings(path=None) -> dict:
    """Real environment wins over the file, so systemd's EnvironmentFile and a
    local .env cannot disagree about which is authoritative.

    Every known key passes through. An earlier version filtered against a list
    that omitted BASE_URL, which silently dropped it and would have sent every
    subscriber a config link pointing at the wrong host.
    """
    merged = load_env_file(path)
    merged.update({k: v for k, v in os.environ.items() if k in KNOWN})
    return merged


def require(values: dict, *keys) -> None:
    missing = [key for key in keys if not values.get(key)]
    if missing:
        raise SystemExit("missing configuration: %s" % ", ".join(missing))
