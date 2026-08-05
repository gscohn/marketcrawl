"""Logging setup.

A `logging.getLogger(...)` with nothing configured discards every INFO record
silently. The command log added for spec section 12 wrote nothing at all until
this existed, which made the privacy policy's claim about what reaches the
server log untrue.

Output goes to stdout because systemd captures it into journald, which already
adds timestamps and unit names.
"""

from __future__ import annotations

import logging
import sys

CONFIGURED = False


def configure(level=logging.INFO) -> None:
    """Idempotent: gunicorn imports the app in several workers."""
    global CONFIGURED
    if CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    root = logging.getLogger("sportscrawl")
    root.setLevel(level)
    root.addHandler(handler)
    # Otherwise every record is also handed to the root logger, which in
    # gunicorn is already writing to the same stream.
    root.propagate = False

    CONFIGURED = True
