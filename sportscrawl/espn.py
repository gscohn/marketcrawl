"""ESPN scoreboard parsing.

Parsing only — no network, no cache. Those arrive in build step 2. Keeping the
parse pure is what lets the formatter be tested against saved JSON.

The adapter boundary exists because this API is undocumented and unversioned.
One of the three endpoints named in the original spec draft was already a 404
by the time code was written; see spec section 5.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import Event, Team


class ParseError(Exception):
    """The response was not a scoreboard we recognise.

    Distinct from a fetch failure, but the caller treats both as a league
    failure: a 200 that fails validation is a failure, not an empty slate.
    Spec section 5.
    """


def _parse_start(raw: str) -> datetime:
    """ESPN stamps events as '2026-08-03T22:40Z'. Always UTC, minute precision."""
    return datetime.strptime(raw, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def _parse_team(competitor: dict) -> Team:
    team = competitor.get("team") or {}
    # shortDisplayName is the digest-sized name: "Phillies", "LA Galaxy".
    name = team.get("shortDisplayName") or team.get("displayName")
    if not name:
        raise ParseError("competitor has no usable team name")
    return Team(
        team_id=str(team.get("id", "")),
        name=name,
        abbrev=team.get("abbreviation") or "",
    )


def _parse_score(competitor: dict) -> int | None:
    raw = competitor.get("score")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_scoreboard(payload: dict, league: str) -> list[Event]:
    """Turn one scoreboard response into Events.

    Raises ParseError on a malformed response. An empty `events` list is a
    valid, meaningful answer: no games.
    """
    if not isinstance(payload, dict) or "events" not in payload:
        raise ParseError("no events key in payload")

    events = []
    for raw in payload["events"]:
        try:
            competitions = raw["competitions"]
            competitors = competitions[0]["competitors"]
            by_side = {c["homeAway"]: c for c in competitors}
            home_raw, away_raw = by_side["home"], by_side["away"]

            # State, not status name. MLB reports STATUS_FINAL and MLS reports
            # STATUS_FULL_TIME for the same condition; both are state "post".
            state = raw["status"]["type"]["state"]

            winner = None
            if home_raw.get("winner"):
                winner = "home"
            elif away_raw.get("winner"):
                winner = "away"

            events.append(
                Event(
                    event_id=str(raw["id"]),
                    league=league,
                    start=_parse_start(raw["date"]),
                    state=state,
                    home=_parse_team(home_raw),
                    away=_parse_team(away_raw),
                    home_score=_parse_score(home_raw),
                    away_score=_parse_score(away_raw),
                    winner=winner,
                )
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ParseError("malformed event: %s" % exc) from exc

    return events


def load_fixture(path: str | Path, league: str) -> list[Event]:
    """Read a saved scoreboard from disk.

    encoding is explicit: the dev box is Windows, where the default is cp1252
    and team names like "CF Montreal" blow up on load.
    """
    with open(path, encoding="utf-8") as handle:
        return parse_scoreboard(json.load(handle), league)
