"""Liveness tests.

The scheduler once ran for eight hours, alive by every check systemd makes,
delivering nothing and logging nothing. These cover the two things that would
have made that visible and self-correcting.
"""

from __future__ import annotations

import time

from sportscrawl.watchdog import Liveness


def test_the_first_completed_tick_always_logs(capsys):
    """systemd's "Started" only means the process spawned. This is the line
    that says the loop actually turned."""
    live = Liveness("test", heartbeat=3600)
    live.tick()
    assert "test alive: 1 tick" in capsys.readouterr().out


def test_after_the_first_tick_heartbeats_wait_for_the_interval(capsys):
    """A line a minute for a service that is usually idle is noise, and noise
    is what stops anyone reading the log."""
    live = Liveness("test", heartbeat=3600)
    for _ in range(10):
        live.tick()
    assert capsys.readouterr().out.count("alive") == 1


def test_a_heartbeat_repeats_once_the_interval_passes(capsys):
    live = Liveness("test", heartbeat=0)
    for _ in range(3):
        live.tick()
    assert capsys.readouterr().out.count("alive") == 3


def test_the_watchdog_exits_a_stalled_process():
    """Restart=always only helps if the process actually dies."""
    stalled = []
    live = Liveness("test", limit=0.05, poll=0.01, on_stall=stalled.append)
    live.start_watchdog()

    deadline = time.monotonic() + 2
    while not stalled and time.monotonic() < deadline:
        time.sleep(0.02)

    assert stalled == [1]


def test_the_watchdog_stays_quiet_while_ticks_continue():
    stalled = []
    live = Liveness("test", limit=0.5, poll=0.01, on_stall=stalled.append)
    live.start_watchdog()

    for _ in range(10):
        time.sleep(0.02)
        live.tick()

    assert stalled == []
