"""Core value types.

Target is Python 3.9 on the server (AlmaLinux 8 AppStream), so the future
import is load-bearing: it keeps `str | None` annotations from being evaluated
at runtime. See section 13 of the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Emoji identify the sport, not the league. Collisions are intentional:
# both basketball leagues share one, every soccer competition shares one.
# Spec section 6.
SPORT_EMOJI = {
    "football": "\N{AMERICAN FOOTBALL}",
    "basketball": "\N{BASKETBALL AND HOOP}",
    "baseball": "\N{BASEBALL}",
    "hockey": "\N{ICE HOCKEY STICK AND PUCK}",
    "soccer": "\N{SOCCER BALL}",
}


@dataclass(frozen=True)
class Team:
    team_id: str
    name: str
    abbrev: str


@dataclass(frozen=True)
class Event:
    """One game, as the formatter needs it. Times are always aware UTC."""

    event_id: str
    league: str  # ESPN path slug, e.g. "baseball/mlb"
    start: datetime
    state: str  # "pre", "in", "post"
    home: Team
    away: Team
    home_score: int | None
    away_score: int | None
    winner: str | None  # "home", "away", or None for a draw or unfinished game

    @property
    def sport(self) -> str:
        return self.league.split("/", 1)[0]

    @property
    def emoji(self) -> str:
        return SPORT_EMOJI[self.sport]

    @property
    def is_final(self) -> bool:
        return self.state == "post"

    def local_date(self, zone):
        """The local date a game belongs to is the date it STARTS.

        A 22:00 local start belongs to that day no matter when it ends.
        Spec section 6, "Which day is a game on".
        """
        return self.start.astimezone(zone).date()


@dataclass(frozen=True)
class Sub:
    kind: str  # "team" or "competition"
    league: str
    team_id: str | None = None


@dataclass(frozen=True)
class Digest:
    text: str | None  # None means send nothing
    kind: str  # "digest", "partial", "failure", "silent"
