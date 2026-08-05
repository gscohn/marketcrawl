"""Formatter tests. Spec section 15, priority order.

Real ESPN fixtures where the shape of live data is the thing under test, and
synthetic events where a precise clock is (DST, a 22:00 start, an early kickoff
already final at a late send time).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from sportscrawl.espn import ParseError, load_fixture, parse_scoreboard
from sportscrawl.formatter import (
    FAILURE_NOTICE,
    HANDLE,
    MAX_MESSAGE,
    MISSING_DATA_LINE,
    format_digest,
)
from sportscrawl.models import Event, Sub, Team

FIXTURES = Path(__file__).parent / "fixtures" / "espn"

MLB = "baseball/mlb"
MLS = "soccer/usa.1"
NFL = "football/nfl"
WORLD_CUP = "soccer/fifa.world"

PACIFIC = "America/Los_Angeles"
EASTERN = "America/New_York"
CENTRAL = "America/Chicago"
MOUNTAIN = "America/Denver"

DODGERS = "19"


def fixture(name: str, league: str):
    return load_fixture(FIXTURES / name, league)


def team(team_id: str, name: str) -> Team:
    return Team(team_id=team_id, name=name, abbrev=name[:3].upper())


def game(
    league=MLB,
    start=None,
    state="pre",
    home=None,
    away=None,
    home_score=None,
    away_score=None,
    winner=None,
    event_id="1",
) -> Event:
    return Event(
        event_id=event_id,
        league=league,
        start=start or datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
        state=state,
        home=home or team("100", "Home"),
        away=away or team("200", "Away"),
        home_score=home_score,
        away_score=away_score,
        winner=winner,
    )


def at_local(tz: str, year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))


# --------------------------------------------------------------------------
# Empty slate and silence
# --------------------------------------------------------------------------


def test_no_subscriptions_is_silent():
    result = format_digest([], [], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7))
    assert result.kind == "silent"
    assert result.text is None


def test_empty_slate_sends_nothing():
    """An NFL-only subscriber in August must not hear 'no games today'."""
    events = fixture("nfl_20260804.json", NFL)
    assert events == []

    result = format_digest(
        events, [Sub("team", NFL, "12")], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7)
    )
    assert result.kind == "silent"
    assert result.text is None


def test_league_has_games_but_none_are_followed():
    events = fixture("mlb_20260803.json", MLB)
    result = format_digest(
        events,
        [Sub("team", MLB, "no-such-team")],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
    )
    assert result.kind == "silent"


# --------------------------------------------------------------------------
# A single game, end to end
# --------------------------------------------------------------------------


def test_single_final_renders_header_section_and_attribution():
    events = [
        game(
            start=datetime(2026, 8, 4, 2, 10, tzinfo=timezone.utc),  # Aug 3, 7:10p PT
            state="post",
            home=team("100", "Dodgers"),
            away=team("200", "Padres"),
            home_score=6,
            away_score=3,
            winner="home",
        )
    ]
    result = format_digest(
        events, [Sub("team", MLB, "100")], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7)
    )

    assert result.kind == "digest"
    assert result.text == "\n".join(
        [
            "Tue Aug 4",
            "",
            "Final",
            "\N{BASEBALL} Dodgers 6, Padres 3",
            "",
            HANDLE,
        ]
    )


def test_scheduled_game_uses_vs_at_and_local_clock():
    events = [
        game(
            start=datetime(2026, 8, 5, 2, 30, tzinfo=timezone.utc),  # Aug 4, 7:30p PT
            home=team("100", "Lakers"),
            away=team("200", "Nuggets"),
            league="basketball/nba",
        )
    ]
    result = format_digest(
        events,
        [Sub("team", "basketball/nba", "100")],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
    )
    assert "\N{BASKETBALL AND HOOP} Lakers vs Nuggets 7:30p" in result.text


def test_no_followed_team_uses_vs_with_the_home_side_first():
    """`at` asserts a venue, and competition slates are full of neutral sites
    where that assertion is false. `vs` is never wrong."""
    events = [
        game(
            league=WORLD_CUP,
            start=at_local(PACIFIC, 2026, 8, 4, 16, 0).astimezone(timezone.utc),
            home=team("100", "Dream"),
            away=team("200", "Mercury"),
        )
    ]
    result = format_digest(
        events,
        [Sub("competition", WORLD_CUP)],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
    )
    assert "Dream vs Mercury 4:00p" in result.text
    assert " at " not in result.text


def test_away_team_takes_at():
    events = [
        game(
            start=datetime(2026, 8, 5, 0, 30, tzinfo=timezone.utc),  # Aug 4, 5:30p PT
            home=team("100", "Austin FC"),
            away=team("200", "Galaxy"),
            league=MLS,
        )
    ]
    result = format_digest(
        events, [Sub("team", MLS, "200")], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7)
    )
    assert "\N{SOCCER BALL} Galaxy at Austin FC 5:30p" in result.text


# --------------------------------------------------------------------------
# Which day is a game on
# --------------------------------------------------------------------------


def test_late_pacific_game_espn_buckets_to_the_next_utc_day():
    """The gotcha in spec section 5.

    SF at TEX starts 2026-08-04T00:00Z, which is Aug 3 5:00p Pacific. ESPN
    files it under the Aug 3 scoreboard. Fetching only "today" would drop it.
    """
    events = fixture("mlb_20260803.json", MLB)
    late = [e for e in events if e.start.day == 4]
    assert late, "fixture should contain games stamped on the following UTC day"

    zone = ZoneInfo(PACIFIC)
    assert all(e.local_date(zone).day == 3 for e in late)

    result = format_digest(
        events,
        [Sub("competition", MLB)],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
    )
    assert "Final" in result.text
    for event in late:
        assert event.home.name in result.text


@pytest.mark.parametrize("tz", [PACIFIC, MOUNTAIN, CENTRAL, EASTERN])
def test_date_boundary_across_us_timezones(tz):
    """One kickoff, four subscribers, each seeing their own local date.

    2026-08-04T02:00Z is Aug 3 in every US zone, so every subscriber reading
    on Aug 4 gets it as a final from yesterday.
    """
    events = [
        game(
            start=datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
            state="post",
            home=team("100", "Home"),
            away=team("200", "Away"),
            home_score=2,
            away_score=1,
            winner="home",
        )
    ]
    result = format_digest(
        events, [Sub("team", MLB, "100")], tz, at_local(tz, 2026, 8, 4, 7)
    )
    assert result.kind == "digest"
    assert "Final" in result.text


def test_game_starting_before_midnight_belongs_to_the_day_it_started():
    """A 22:00 local start that ends after midnight is still yesterday's game."""
    events = [
        game(
            start=at_local(PACIFIC, 2026, 8, 3, 22, 0).astimezone(timezone.utc),
            state="post",
            home=team("100", "Home"),
            away=team("200", "Away"),
            home_score=4,
            away_score=2,
            winner="home",
        )
    ]
    result = format_digest(
        events, [Sub("team", MLB, "100")], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7)
    )
    assert "Final" in result.text
    assert "Today" not in result.text


def test_early_kickoff_already_final_at_a_late_send_time():
    """A 10am kickoff is a final, not a fixture, to someone reading at 6pm."""
    events = [
        game(
            league=MLS,
            start=at_local(EASTERN, 2026, 8, 4, 10, 0).astimezone(timezone.utc),
            state="post",
            home=team("100", "Home"),
            away=team("200", "Away"),
            home_score=1,
            away_score=0,
            winner="home",
        )
    ]
    result = format_digest(
        events, [Sub("team", MLS, "100")], EASTERN, at_local(EASTERN, 2026, 8, 4, 18)
    )
    assert "Final" in result.text
    assert "Today" not in result.text


def test_yesterdays_unplayed_game_is_dropped():
    """Postponed and still sitting in the feed. A stale fixture line is worse
    than a missing one."""
    events = [
        game(
            start=at_local(PACIFIC, 2026, 8, 3, 19, 0).astimezone(timezone.utc),
            state="pre",
        )
    ]
    result = format_digest(
        events, [Sub("team", MLB, "100")], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7)
    )
    assert result.kind == "silent"


def test_in_progress_game_shows_no_score():
    events = [
        game(
            start=at_local(PACIFIC, 2026, 8, 4, 13, 0).astimezone(timezone.utc),
            state="in",
            home=team("100", "Home"),
            away=team("200", "Away"),
            home_score=3,
            away_score=1,
        )
    ]
    result = format_digest(
        events, [Sub("team", MLB, "100")], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7)
    )
    assert "Today" in result.text
    # The line carries a start time and nothing else. No 3-1.
    assert "\N{BASEBALL} Home vs Away 1:00p" in result.text
    assert "3" not in result.text


# --------------------------------------------------------------------------
# DST
# --------------------------------------------------------------------------


def test_spring_forward_day_renders_local_clock_correctly():
    """2026-03-08 is the US spring forward. A 7:30p Pacific first pitch that
    evening is 02:30Z on the 9th, and must still read 7:30p."""
    start = at_local(PACIFIC, 2026, 3, 8, 19, 30).astimezone(timezone.utc)
    assert start == datetime(2026, 3, 9, 2, 30, tzinfo=timezone.utc)

    events = [game(start=start, home=team("100", "Home"), away=team("200", "Away"))]
    result = format_digest(
        events, [Sub("team", MLB, "100")], PACIFIC, at_local(PACIFIC, 2026, 3, 8, 7)
    )
    assert "7:30p" in result.text


def test_day_after_dst_change_still_reports_yesterday():
    events = [
        game(
            start=at_local(PACIFIC, 2026, 3, 8, 19, 30).astimezone(timezone.utc),
            state="post",
            home=team("100", "Home"),
            away=team("200", "Away"),
            home_score=5,
            away_score=4,
            winner="home",
        )
    ]
    result = format_digest(
        events, [Sub("team", MLB, "100")], PACIFIC, at_local(PACIFIC, 2026, 3, 9, 7)
    )
    assert "Mon Mar 9" in result.text
    assert "Final" in result.text


# --------------------------------------------------------------------------
# Which team goes first
# --------------------------------------------------------------------------


def test_followed_team_leads_even_in_a_loss():
    events = [
        game(
            start=datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
            state="post",
            home=team("100", "Cubs"),
            away=team("200", "Dodgers"),
            home_score=10,
            away_score=5,
            winner="home",
        )
    ]
    result = format_digest(
        events, [Sub("team", MLB, "200")], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7)
    )
    assert "Dodgers 5, Cubs 10" in result.text


def test_winner_leads_when_no_team_is_followed():
    events = [
        game(
            league=WORLD_CUP,
            start=datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
            state="post",
            home=team("100", "Chelsea"),
            away=team("200", "Arsenal"),
            home_score=1,
            away_score=2,
            winner="away",
        )
    ]
    result = format_digest(
        events,
        [Sub("competition", WORLD_CUP)],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
    )
    assert "Arsenal 2, Chelsea 1" in result.text


def test_draw_puts_the_home_team_first():
    events = [
        game(
            league=WORLD_CUP,
            start=datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
            state="post",
            home=team("100", "Vancouver"),
            away=team("200", "LAFC"),
            home_score=1,
            away_score=1,
            winner=None,
        )
    ]
    result = format_digest(
        events,
        [Sub("competition", WORLD_CUP)],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
    )
    assert "Vancouver 1, LAFC 1" in result.text


def test_both_teams_followed_puts_home_first():
    events = [
        game(
            start=datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
            state="post",
            home=team("100", "Giants"),
            away=team("200", "Athletics"),
            home_score=3,
            away_score=7,
            winner="away",
        )
    ]
    result = format_digest(
        events,
        [Sub("team", MLB, "100"), Sub("team", MLB, "200")],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
    )
    assert "Giants 3, Athletics 7" in result.text


# --------------------------------------------------------------------------
# Competition subscriptions
# --------------------------------------------------------------------------


def test_competition_sub_includes_tomorrow():
    """Real World Cup group day, matches reset to unplayed.

    The fixture is historical so every match in it is already final, and a
    final is never a fixture for tomorrow. Resetting state keeps the real
    kickoff times and team names while testing the lookahead rule.
    """
    played = fixture("fifa_world_20260624.json", WORLD_CUP)
    assert len(played) == 6
    events = [replace(event, state="pre", winner=None) for event in played]

    zone = ZoneInfo(EASTERN)
    match_day = events[0].local_date(zone)
    now = datetime.combine(
        match_day - timedelta(days=1), datetime.min.time(), tzinfo=zone
    ) + timedelta(hours=7)

    result = format_digest(events, [Sub("competition", WORLD_CUP)], EASTERN, now)
    assert "Tomorrow" in result.text
    assert result.text.count("\N{SOCCER BALL}") == 6


def test_tomorrows_fixtures_are_not_labelled_final():
    played = fixture("fifa_world_20260624.json", WORLD_CUP)
    events = [replace(event, state="pre", winner=None) for event in played]
    zone = ZoneInfo(EASTERN)
    match_day = events[0].local_date(zone)
    now = datetime.combine(
        match_day - timedelta(days=1), datetime.min.time(), tzinfo=zone
    ) + timedelta(hours=7)

    result = format_digest(events, [Sub("competition", WORLD_CUP)], EASTERN, now)
    assert "Final" not in result.text


def test_team_sub_does_not_include_tomorrow():
    """A day's notice is useful for a tournament, not for a 162 game season."""
    events = [
        game(
            start=at_local(PACIFIC, 2026, 8, 5, 19, 0).astimezone(timezone.utc),
            home=team("100", "Home"),
            away=team("200", "Away"),
        )
    ]
    result = format_digest(
        events, [Sub("team", MLB, "100")], PACIFIC, at_local(PACIFIC, 2026, 8, 4, 7)
    )
    assert result.kind == "silent"


# --------------------------------------------------------------------------
# Overflow
# --------------------------------------------------------------------------


def test_full_mls_saturday_fits_and_stays_under_the_cap():
    events = fixture("mls_20260801.json", MLS)
    assert len(events) == 14

    result = format_digest(
        events, [Sub("competition", MLS)], EASTERN, at_local(EASTERN, 2026, 8, 2, 7)
    )
    assert result.kind == "digest"
    assert len(result.text) <= MAX_MESSAGE
    assert result.text.count("\N{SOCCER BALL}") == 14


def test_overflow_drops_lines_by_priority_and_reports_a_count(monkeypatch):
    """Priority ordering, not byte truncation. Followed finals survive."""
    monkeypatch.setattr("sportscrawl.formatter.MAX_MESSAGE", 220)

    events = fixture("mls_20260801.json", MLS)
    followed = events[0].home.team_id
    subs = [Sub("competition", MLS), Sub("team", MLS, followed)]

    result = format_digest(
        events, subs, EASTERN, at_local(EASTERN, 2026, 8, 2, 7)
    )
    assert len(result.text) <= 220
    assert "more" in result.text
    assert events[0].home.name in result.text  # the followed final survived
    assert result.text.endswith(HANDLE)


# --------------------------------------------------------------------------
# Failure handling
# --------------------------------------------------------------------------


def test_total_failure_returns_a_notice():
    result = format_digest(
        [],
        [Sub("team", MLB, DODGERS)],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
        failed_leagues={MLB},
    )
    assert result.kind == "failure"
    assert result.text.startswith(FAILURE_NOTICE)
    assert "ESPN" not in result.text
    assert "error" not in result.text.lower()


def test_partial_failure_still_sends_the_healthy_league():
    """Only soccer failed, so the baseball subscriber still gets baseball."""
    events = [
        game(
            start=datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
            state="post",
            home=team("100", "Dodgers"),
            away=team("200", "Padres"),
            home_score=6,
            away_score=3,
            winner="home",
        )
    ]
    result = format_digest(
        events,
        [Sub("team", MLB, "100"), Sub("competition", MLS)],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
        failed_leagues={MLS},
    )
    assert result.kind == "partial"
    assert "Dodgers 6, Padres 3" in result.text
    assert MISSING_DATA_LINE in result.text


def test_failed_league_events_are_never_rendered():
    """Stale cached events for a failing league must not leak into a digest."""
    events = [
        game(
            league=MLS,
            start=datetime(2026, 8, 4, 2, 0, tzinfo=timezone.utc),
            state="post",
            home=team("100", "Galaxy"),
            away=team("200", "Austin FC"),
            home_score=2,
            away_score=0,
            winner="home",
        )
    ]
    result = format_digest(
        events,
        [Sub("competition", MLS), Sub("team", MLB, DODGERS)],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
        failed_leagues={MLS},
    )
    assert "Galaxy" not in (result.text or "")


def test_failure_wins_when_the_healthy_league_has_no_games():
    """One league down, the other simply empty. Silence would be a lie here:
    there is a league we could not check."""
    result = format_digest(
        [],
        [Sub("team", MLB, DODGERS), Sub("competition", MLS)],
        PACIFIC,
        at_local(PACIFIC, 2026, 8, 4, 7),
        failed_leagues={MLS},
    )
    assert result.kind == "failure"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_empty_events_is_not_an_error():
    assert parse_scoreboard({"events": []}, MLB) == []


def test_missing_events_key_is_a_parse_error():
    with pytest.raises(ParseError):
        parse_scoreboard({"leagues": []}, MLB)


def test_malformed_event_is_a_parse_error():
    with pytest.raises(ParseError):
        parse_scoreboard({"events": [{"id": "1"}]}, MLB)


def test_state_not_status_name_decides_finality():
    """MLB says STATUS_FINAL, MLS says STATUS_FULL_TIME, both are state post."""
    mlb = fixture("mlb_20260803.json", MLB)
    mls = fixture("mls_20260801.json", MLS)
    assert all(e.is_final for e in mlb)
    assert all(e.is_final for e in mls)


def test_accented_team_names_survive_the_round_trip():
    """CF Montreal carries a non-ASCII character. The dev box defaults to
    cp1252, so this fails loudly if an encoding is ever left implicit."""
    events = fixture("mls_20260801.json", MLS)
    names = {e.home.name for e in events} | {e.away.name for e in events}
    assert any("é" in name for name in names)
