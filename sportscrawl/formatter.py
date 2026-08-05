"""The digest formatter.

A pure function. No network, no database, no Bluesky. Every hard problem in
this project lives here, which is why it is built first and tested against
saved fixtures. Spec section 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Digest, Event, Sub

HANDLE = "@sportscrawl.app"

# Verified 2026-08-04 against the live API, not guessed: sendMessage rejects
# anything longer with
#   "grapheme too big (maximum 1000, got 4960) at $.message.text"
#
# Graphemes, not codepoints. len() counts codepoints, which over-counts a
# decomposed accent and never under-counts for anything a digest line can
# hold. That errs toward trimming a line early rather than having the whole
# message rejected at send time, which is the right direction to be wrong in.
MAX_MESSAGE = 1000

MISSING_DATA_LINE = "Some scores are missing."

# No retry promises, nothing technical. Spec section 7.
FAILURE_NOTICE = "No scores available right now. The data source isn't responding."

# Overflow priority, lowest number wins. Spec section 6, "Length".
TIER_FOLLOWED_FINAL = 1
TIER_TODAY = 2
TIER_OTHER = 3


@dataclass(frozen=True)
class _Line:
    tier: int
    section: str
    start: datetime
    text: str


def _clock(when: datetime) -> str:
    """5:30p, 7:05a. No leading zero on the hour, no space, no meridiem dots."""
    hour = when.hour % 12 or 12
    return "%d:%02d%s" % (hour, when.minute, "a" if when.hour < 12 else "p")


def _header(today: date) -> str:
    """Tue Aug 4. Built by hand because %-d is not portable to Windows."""
    return "%s %s %d" % (
        today.strftime("%a"),
        today.strftime("%b"),
        today.day,
    )


def _followed_team_ids(subs, league: str):
    return {s.team_id for s in subs if s.kind == "team" and s.league == league}


def _order(event: Event, followed: set):
    """Which team is named first, and the connector for a scheduled game.

    One deterministic rule covering every case. Spec section 6, "Which team
    goes first":

      1. exactly one competitor followed -> that team leads
      2. both competitors followed -> the home team leads
      3. otherwise, a final with a winner -> the winner leads
      4. otherwise -> the home team leads

    Order matters: rule 2 has to be tested before rule 3, or a fan of both Bay
    Area teams gets the winner first instead of the neutral venue ordering.

    The connector then falls out of the choice: a leading home team takes
    "vs", a leading away team takes "at".
    """
    home_followed = event.home.team_id in followed
    away_followed = event.away.team_id in followed

    if home_followed and away_followed:
        lead_is_home = True
    elif home_followed:
        lead_is_home = True
    elif away_followed:
        lead_is_home = False
    elif event.is_final and event.winner is not None:
        lead_is_home = event.winner == "home"
    else:
        lead_is_home = True

    if lead_is_home:
        return event.home, event.away, "vs"
    return event.away, event.home, "at"


def _final_line(event: Event, followed: set) -> str:
    first, second, _ = _order(event, followed)
    first_score = event.home_score if first is event.home else event.away_score
    second_score = event.home_score if second is event.home else event.away_score
    return "%s %s %s, %s %s" % (
        event.emoji,
        first.name,
        first_score,
        second.name,
        second_score,
    )


def _scheduled_line(event: Event, followed: set, zone) -> str:
    first, second, connector = _order(event, followed)
    return "%s %s %s %s %s" % (
        event.emoji,
        first.name,
        connector,
        second.name,
        _clock(event.start.astimezone(zone)),
    )


def _relevant(event: Event, subs, team_leagues, competition_leagues) -> bool:
    if event.league in competition_leagues:
        return True
    if event.league in team_leagues:
        followed = team_leagues[event.league]
        return event.home.team_id in followed or event.away.team_id in followed
    return False


def _build_lines(events, subs, zone, today) -> list:
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    team_leagues = {}
    competition_leagues = set()
    for sub in subs:
        if sub.kind == "competition":
            competition_leagues.add(sub.league)
        else:
            team_leagues.setdefault(sub.league, set()).add(sub.team_id)

    lines = []
    for event in events:
        if not _relevant(event, subs, team_leagues, competition_leagues):
            continue

        local = event.local_date(zone)
        followed = team_leagues.get(event.league, set())
        is_competition = event.league in competition_leagues

        if local == yesterday:
            # Yesterday's games that never went final -- postponed, abandoned,
            # still sitting there -- are dropped. A stale fixture line is worse
            # than a missing one.
            if not event.is_final:
                continue
            lines.append(
                _Line(
                    tier=TIER_FOLLOWED_FINAL if followed else TIER_OTHER,
                    section="Final",
                    start=event.start,
                    text=_final_line(event, followed),
                )
            )
        elif local == today:
            if event.is_final:
                # An early kickoff already over by a late send_time belongs
                # under Final, not Today.
                lines.append(
                    _Line(
                        tier=TIER_FOLLOWED_FINAL if followed else TIER_OTHER,
                        section="Final",
                        start=event.start,
                        text=_final_line(event, followed),
                    )
                )
            else:
                # In progress games are listed with their start time and no
                # score. A live score is stale minutes later and section 2
                # rules out real-time anyway.
                lines.append(
                    _Line(
                        tier=TIER_TODAY,
                        section="Today",
                        start=event.start,
                        text=_scheduled_line(event, followed, zone),
                    )
                )
        elif local == tomorrow and is_competition:
            # Tournament matches cluster and a day's notice is useful there in
            # a way it is not for a 162 game season.
            if event.is_final:
                continue
            lines.append(
                _Line(
                    tier=TIER_OTHER,
                    section="Tomorrow",
                    start=event.start,
                    text=_scheduled_line(event, followed, zone),
                )
            )

    lines.sort(key=lambda line: (line.start, line.text))
    return lines


def _render(lines, today, trailer_lines) -> str:
    out = [_header(today)]
    for section in ("Final", "Today", "Tomorrow"):
        section_lines = [line.text for line in lines if line.section == section]
        if section_lines:
            out.append("")
            out.append(section)
            out.extend(section_lines)
    for trailer in trailer_lines:
        out.append("")
        out.append(trailer)
    return "\n".join(out)


def _fit(lines, today, trailer_lines) -> str:
    """Trim to the message cap by priority, not by byte truncation.

    Drops whole lines starting from the lowest priority tier and the latest
    start time, then reports the count. Spec section 6.
    """
    kept = list(lines)
    dropped = 0
    text = _render(kept, today, trailer_lines)

    while len(text) > MAX_MESSAGE and kept:
        victim = max(kept, key=lambda line: (line.tier, line.start))
        kept.remove(victim)
        dropped += 1
        text = _render(kept, today, ["+%d more" % dropped] + trailer_lines)

    return text


def format_digest(
    events,
    subs,
    tz: str,
    now: datetime,
    failed_leagues=frozenset(),
) -> Digest:
    """Assemble one subscriber's digest.

    `failed_leagues` is required: without it the function cannot tell "no games
    in MLS" from "MLS did not answer", and section 7 needs that distinction to
    decide between a digest, an apology, and silence.
    """
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()

    subscribed = {sub.league for sub in subs}
    if not subscribed:
        return Digest(text=None, kind="silent")

    failed = subscribed & set(failed_leagues)
    if failed == subscribed:
        # Nothing to attach a digest to.
        return Digest(text=FAILURE_NOTICE + "\n\n" + HANDLE, kind="failure")

    usable = [event for event in events if event.league not in failed]
    lines = _build_lines(usable, subs, zone, today)

    if not lines:
        # No games is not an error and gets no message. An NFL-only subscriber
        # would otherwise hear "no games today" for seven months.
        if failed:
            return Digest(text=FAILURE_NOTICE + "\n\n" + HANDLE, kind="failure")
        return Digest(text=None, kind="silent")

    trailer = [MISSING_DATA_LINE, HANDLE] if failed else [HANDLE]
    return Digest(
        text=_fit(lines, today, trailer),
        kind="partial" if failed else "digest",
    )
