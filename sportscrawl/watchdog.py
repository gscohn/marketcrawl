"""Liveness for the long-running loops.

The scheduler ran for eight hours, held the right database, logged nothing,
and delivered nothing. It was alive by every check systemd makes. A restart
fixed it and destroyed the only evidence of why.

Two things follow. A loop that is idle must look different from a loop that is
stuck, which is what the heartbeat is for. And a stuck loop must not wait for
someone to notice, which is what the watchdog is for: `Restart=always` only
helps if the process actually dies.
"""

from __future__ import annotations

import os
import sys
import threading
import time

HEARTBEAT_SECONDS = 900  # 15 minutes
WATCHDOG_LIMIT = 300  # a tick must complete at least this often
WATCHDOG_POLL = 30


def _hard_exit(code):
    # _exit rather than sys.exit: the point is that the process is wedged
    # somewhere, so anything that waits on the stuck thread would hang here too.
    os._exit(code)


class Liveness:
    def __init__(
        self,
        name,
        heartbeat=HEARTBEAT_SECONDS,
        limit=WATCHDOG_LIMIT,
        poll=WATCHDOG_POLL,
        on_stall=_hard_exit,
    ):
        self.name = name
        self._heartbeat = heartbeat
        self._limit = limit
        self._poll = poll
        self._on_stall = on_stall
        self._last_tick = time.monotonic()
        self._last_beat = 0.0
        self._ticks = 0

    def tick(self) -> None:
        """Call once per completed loop iteration."""
        self._last_tick = time.monotonic()
        self._ticks += 1

        now = time.monotonic()
        # The first completed tick always logs. systemd's "Started" only means
        # the process spawned; this is the line that says the loop actually
        # turned, which is the distinction that went unnoticed for eight hours.
        first = self._ticks == 1
        if first or now - self._last_beat >= self._heartbeat:
            self._last_beat = now
            print(
                "%s alive: %d tick%s"
                % (self.name, self._ticks, "" if self._ticks == 1 else "s"),
                flush=True,
            )

    def start_watchdog(self) -> None:
        def watch():
            while True:
                time.sleep(self._poll)
                stalled = time.monotonic() - self._last_tick
                if stalled > self._limit:
                    print(
                        "%s watchdog: no tick in %.1fs, exiting for restart"
                        % (self.name, stalled),
                        file=sys.stderr,
                        flush=True,
                    )
                    self._on_stall(1)
                    return

        thread = threading.Thread(target=watch, daemon=True, name="watchdog")
        thread.start()
