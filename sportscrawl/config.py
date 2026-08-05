"""Environment configuration.

Secrets live in an env file outside the repo, mode 600, referenced from the
systemd units with EnvironmentFile=. Locally a .env sits alongside the code
and is gitignored. Spec section 13.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env_file(path=".env") -> dict:
    """Parse a KEY=value file. Absent file is not an error."""
    values = {}
    location = Path(path)
    if not location.exists():
        return values

    for line in location.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def settings(path=".env") -> dict:
    """Real environment wins over the file, so systemd's EnvironmentFile and a
    local .env cannot disagree about which is authoritative."""
    merged = load_env_file(path)
    merged.update({k: v for k, v in os.environ.items() if k in REQUIRED or k in OPTIONAL})
    return merged


REQUIRED = ("BSKY_IDENTIFIER", "BSKY_APP_PASSWORD", "EVENTS_PEPPER")
OPTIONAL = ("DATABASE_PATH", "ADMIN_DID", "ADMIN_PASSWORD", "FLASK_SECRET_KEY")


def require(values: dict, *keys) -> None:
    missing = [key for key in keys if not values.get(key)]
    if missing:
        raise SystemExit("missing configuration: %s" % ", ".join(missing))
