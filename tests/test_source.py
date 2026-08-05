"""Adapter tests. Hermetic: the fake transport means no test touches ESPN."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sportscrawl.espn import ParseError
from sportscrawl.http import FetchError, Fetcher, Response
from sportscrawl.source import EspnSource, window_dates

FIXTURES = Path(__file__).parent / "fixtures" / "espn"

MLB = "baseball/mlb"
MLS = "soccer/usa.1"

NOW = datetime(2026, 8, 4, 14, 0, tzinfo=timezone.utc)


def body(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeTransport:
    """Records calls and replies from a scripted table."""

    def __init__(self, replies=None, default=None):
        self.replies = replies or {}
        self.default = default or Response(200, '{"events": []}')
        self.calls = []

    def __call__(self, url, timeout, headers):
        self.calls.append(url)
        reply = self.replies.get(url, self.default)
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else self.default
        if isinstance(reply, Exception):
            raise reply
        return reply


def source_with(transport, ttl=timedelta(minutes=10)) -> EspnSource:
    return EspnSource(fetcher=Fetcher(transport=transport, sleep=lambda _: None),
                      scoreboard_ttl=ttl)


def scoreboard_url(league, day):
    return "https://site.api.espn.com/apis/site/v2/sports/%s/scoreboard?dates=%s" % (
        league,
        day,
    )


# --------------------------------------------------------------------------
# The three day window
# --------------------------------------------------------------------------


def test_window_is_three_utc_days():
    assert window_dates(NOW) == [
        date(2026, 8, 3),
        date(2026, 8, 4),
        date(2026, 8, 5),
    ]


def test_window_is_computed_in_utc_not_server_local_time():
    """23:30 in Los Angeles is already the next day in UTC, and the window
    must follow UTC. Spec section 5."""
    late = datetime(2026, 8, 4, 6, 30, tzinfo=timezone.utc)  # 23:30 Aug 3 Pacific
    assert window_dates(late) == [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]


def test_fetch_window_asks_for_exactly_three_days_per_league():
    transport = FakeTransport()
    source = source_with(transport)
    source.fetch_window([MLB, MLS], NOW)

    assert len(transport.calls) == 6
    assert scoreboard_url(MLB, "20260803") in transport.calls
    assert scoreboard_url(MLB, "20260804") in transport.calls
    assert scoreboard_url(MLB, "20260805") in transport.calls


def test_fan_out_is_per_league_not_per_subscriber():
    """A hundred subscribers should still cost single digit API calls."""
    transport = FakeTransport()
    source = source_with(transport)

    source.fetch_window([MLB], NOW)
    for _ in range(100):
        source.fetch_window([MLB], NOW + timedelta(minutes=1))

    assert len(transport.calls) == 3


def test_cache_expires_after_the_ttl():
    transport = FakeTransport()
    source = source_with(transport, ttl=timedelta(minutes=10))

    source.fetch_window([MLB], NOW)
    source.fetch_window([MLB], NOW + timedelta(minutes=9))
    assert len(transport.calls) == 3

    source.fetch_window([MLB], NOW + timedelta(minutes=11))
    assert len(transport.calls) == 6


def test_window_collects_events_from_every_day():
    transport = FakeTransport(
        replies={
            scoreboard_url(MLB, "20260803"): Response(200, body("mlb_20260803.json")),
            scoreboard_url(MLB, "20260804"): Response(200, body("mlb_20260804.json")),
        }
    )
    source = source_with(transport)
    result = source.fetch_window([MLB], NOW)

    assert len(result.events) == 8 + 15
    assert not result.failed_leagues


# --------------------------------------------------------------------------
# Empty is not failed
# --------------------------------------------------------------------------


def test_empty_slate_is_a_success_not_a_failure():
    transport = FakeTransport(default=Response(200, '{"events": []}'))
    source = source_with(transport)
    result = source.fetch_window([MLB], NOW)

    assert result.events == []
    assert result.failed_leagues == set()
    assert source.health[MLB].consecutive_failures == 0
    assert source.health[MLB].last_success_at == NOW


def test_two_hundred_with_garbage_is_a_failure():
    transport = FakeTransport(default=Response(200, "<html>down for maintenance</html>"))
    source = source_with(transport)
    result = source.fetch_window([MLB], NOW)

    assert result.failed_leagues == {MLB}
    assert source.health[MLB].last_success_at is None


def test_two_hundred_missing_events_key_is_a_failure():
    transport = FakeTransport(default=Response(200, '{"leagues": []}'))
    source = source_with(transport)
    assert source.fetch_window([MLB], NOW).failed_leagues == {MLB}


# --------------------------------------------------------------------------
# Failure is per league
# --------------------------------------------------------------------------


def test_one_league_failing_does_not_affect_another():
    """Only a soccer slug fails, so MLB subscribers still get a digest."""
    transport = FakeTransport(
        replies={
            scoreboard_url(MLS, "20260803"): Response(500, ""),
            scoreboard_url(MLS, "20260804"): Response(500, ""),
            scoreboard_url(MLS, "20260805"): Response(500, ""),
            scoreboard_url(MLB, "20260803"): Response(200, body("mlb_20260803.json")),
        }
    )
    source = source_with(transport)
    result = source.fetch_window([MLB, MLS], NOW)

    assert result.failed_leagues == {MLS}
    assert len(result.events) == 8
    assert all(event.league == MLB for event in result.events)


def test_a_failed_league_contributes_no_events():
    """Half a window is worse than none: it silently drops games."""
    transport = FakeTransport(
        replies={
            scoreboard_url(MLB, "20260803"): Response(200, body("mlb_20260803.json")),
            scoreboard_url(MLB, "20260804"): Response(503, ""),
        }
    )
    source = source_with(transport)
    result = source.fetch_window([MLB], NOW)

    assert result.failed_leagues == {MLB}
    assert result.events == []


def test_losing_only_tomorrow_is_degraded_not_failed():
    """It costs competition subscribers their lookahead and team subscribers
    nothing. Not worth suppressing a whole digest over."""
    transport = FakeTransport(
        replies={
            scoreboard_url(MLB, "20260803"): Response(200, body("mlb_20260803.json")),
            scoreboard_url(MLB, "20260805"): Response(500, ""),
        }
    )
    source = source_with(transport)
    result = source.fetch_window([MLB], NOW)

    assert result.failed_leagues == set()
    assert result.degraded_leagues == {MLB}
    assert len(result.events) == 8


# --------------------------------------------------------------------------
# Health tracking, feeding league_state
# --------------------------------------------------------------------------


def test_consecutive_failures_accumulate_and_reset():
    transport = FakeTransport(default=Response(500, ""))
    source = source_with(transport)

    source.fetch_window([MLB], NOW)
    source.fetch_window([MLB], NOW + timedelta(minutes=20))
    assert source.health[MLB].consecutive_failures == 2
    assert source.health[MLB].last_fetch_at == NOW + timedelta(minutes=20)

    transport.default = Response(200, '{"events": []}')
    source.fetch_window([MLB], NOW + timedelta(minutes=40))
    assert source.health[MLB].consecutive_failures == 0


def test_last_event_date_tracks_the_latest_event_seen():
    """This is what competition auto-close reads. Spec section 10."""
    transport = FakeTransport(
        replies={
            scoreboard_url(MLB, "20260803"): Response(200, body("mlb_20260803.json")),
        }
    )
    source = source_with(transport)
    source.fetch_window([MLB], NOW)

    # The Aug 3 slate includes games stamped 2026-08-04 in UTC.
    assert source.health[MLB].last_event_date == date(2026, 8, 4)


def test_an_empty_league_does_not_advance_last_event_date():
    transport = FakeTransport(default=Response(200, '{"events": []}'))
    source = source_with(transport)
    source.fetch_window(["soccer/fifa.world"], NOW)

    assert source.health["soccer/fifa.world"].last_event_date is None
    assert source.health["soccer/fifa.world"].consecutive_failures == 0


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------


def test_a_transient_error_is_retried_and_succeeds():
    url = scoreboard_url(MLB, "20260803")
    transport = FakeTransport(
        replies={url: [Response(503, ""), Response(200, '{"events": []}')]}
    )
    source = source_with(transport)
    source.fetch_day(MLB, date(2026, 8, 3), NOW)

    assert transport.calls.count(url) == 2


def test_retries_give_up_after_three_attempts():
    transport = FakeTransport(default=Response(503, ""))
    fetcher = Fetcher(transport=transport, sleep=lambda _: None)

    with pytest.raises(FetchError):
        fetcher.get_json("https://example.test/x")
    assert len(transport.calls) == 3


def test_a_timeout_is_retried():
    transport = FakeTransport(default=TimeoutError("read timed out"))
    fetcher = Fetcher(transport=transport, sleep=lambda _: None)

    with pytest.raises(FetchError):
        fetcher.get_json("https://example.test/x")
    assert len(transport.calls) == 3


def test_a_404_is_not_retried():
    """It will not improve by asking again. The /sports/soccer endpoint named
    in the spec draft died exactly this way."""
    transport = FakeTransport(default=Response(404, '{"code":404}'))
    fetcher = Fetcher(transport=transport, sleep=lambda _: None)

    with pytest.raises(FetchError):
        fetcher.get_json("https://example.test/x")
    assert len(transport.calls) == 1


def test_backoff_grows_between_attempts():
    slept = []
    transport = FakeTransport(default=Response(500, ""))
    fetcher = Fetcher(transport=transport, sleep=slept.append)

    with pytest.raises(FetchError):
        fetcher.get_json("https://example.test/x")
    assert slept == [0.5, 1.0]


def test_the_user_agent_identifies_the_bot():
    """ESPN 403s unrecognised agents, so the real client token leads and the
    honest identification follows it. Neither half is optional: drop the
    prefix and every request fails, drop the suffix and nobody reading ESPN's
    logs can tell who this is."""
    seen = {}

    def transport(url, timeout, headers):
        seen.update(headers)
        return Response(200, "{}")

    Fetcher(transport=transport).get_json("https://example.test/x")
    agent = seen["User-Agent"]

    assert agent.startswith("python-requests/")
    assert "sportscrawl/1.0 (+https://sportscrawl.app)" in agent
    assert "Mozilla" not in agent  # not a forged browser


def test_timeouts_are_split_connect_and_read():
    seen = {}

    def transport(url, timeout, headers):
        seen["timeout"] = timeout
        return Response(200, "{}")

    Fetcher(transport=transport).get_json("https://example.test/x")
    assert seen["timeout"] == (5, 10)


# --------------------------------------------------------------------------
# Soccer slug resolution
# --------------------------------------------------------------------------


def test_soccer_slugs_are_resolved_not_hardcoded():
    transport = FakeTransport(
        default=Response(200, body("soccer_leagues_dropdown.json"))
    )
    source = source_with(transport)
    slugs = source.soccer_leagues(NOW)

    assert len(slugs) == 220
    assert slugs["fifa.world"] == "FIFA World Cup"
    assert slugs["usa.1"] == "MLS"
    assert "concacaf.champions" in slugs


def test_soccer_slugs_are_cached_for_a_day():
    transport = FakeTransport(
        default=Response(200, body("soccer_leagues_dropdown.json"))
    )
    source = source_with(transport)

    source.soccer_leagues(NOW)
    source.soccer_leagues(NOW + timedelta(hours=23))
    assert len(transport.calls) == 1

    source.soccer_leagues(NOW + timedelta(hours=25))
    assert len(transport.calls) == 2


def test_stale_slugs_are_served_on_error():
    """A slug that resolved yesterday is almost certainly still right. This
    cache exists so a soccer outage does not also break slug resolution."""
    transport = FakeTransport(
        default=Response(200, body("soccer_leagues_dropdown.json"))
    )
    source = source_with(transport)
    first = source.soccer_leagues(NOW)

    transport.default = Response(500, "")
    later = source.soccer_leagues(NOW + timedelta(days=2))
    assert later == first


def test_slug_failure_with_no_cache_raises():
    transport = FakeTransport(default=Response(500, ""))
    source = source_with(transport)

    with pytest.raises(FetchError):
        source.soccer_leagues(NOW)
