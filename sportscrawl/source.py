"""The ESPN adapter.

Everything that knows ESPN exists lives behind this boundary, so a replacement
source could be swapped in without touching the scheduler. Spec section 5.

Build step 2: three-day UTC window, per-league cache, parse validation, and
per-league health tracking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from .espn import ParseError, parse_scoreboard
from .http import FetchError, Fetcher

SITE_API = "https://site.api.espn.com/apis/site/v2/sports"
DROPDOWN_API = "https://site.api.espn.com/apis/site/v2/leagues/dropdown"

SCOREBOARD_TTL = timedelta(minutes=10)
SLUG_TTL = timedelta(hours=24)

# US pro leagues are fixed slugs. Soccer competitions are resolved at runtime
# from the dropdown endpoint, because competition names and slugs change when
# competitions are rebranded. Spec section 5.
US_PRO_LEAGUES = (
    "football/nfl",
    "basketball/nba",
    "baseball/mlb",
    "hockey/nhl",
    "basketball/wnba",
)

SOCCER_LEAGUES_AT_LAUNCH = (
    "soccer/usa.1",  # MLS
    "soccer/usa.nwsl",  # NWSL
    "soccer/eng.1",  # Premier League
    "soccer/fifa.world",
    "soccer/uefa.champions",
    "soccer/concacaf.champions",
)


@dataclass
class LeagueHealth:
    """Feeds the league_state table in spec section 4."""

    league: str
    last_fetch_at: datetime | None = None
    last_success_at: datetime | None = None
    last_event_date: date | None = None
    consecutive_failures: int = 0


@dataclass
class WindowResult:
    events: list = field(default_factory=list)
    failed_leagues: set = field(default_factory=set)
    degraded_leagues: set = field(default_factory=set)


@dataclass
class _CacheEntry:
    fetched_at: datetime
    events: list


def window_dates(now: datetime) -> list:
    """Yesterday, today, tomorrow, as UTC dates.

    Computed against UTC, never server local time and never a subscriber's
    local date. Three UTC days cover the local yesterday/today/tomorrow span
    for every timezone a subscriber can be in. Spec section 5.
    """
    today = now.astimezone(timezone.utc).date()
    return [today - timedelta(days=1), today, today + timedelta(days=1)]


class EspnSource:
    def __init__(self, fetcher=None, scoreboard_ttl=SCOREBOARD_TTL):
        self._fetcher = fetcher or Fetcher()
        self._ttl = scoreboard_ttl
        self._scoreboards = {}
        self._slugs = None
        self._slugs_fetched_at = None
        self.health = {}

    # -- health -----------------------------------------------------------

    def _health_for(self, league: str) -> LeagueHealth:
        return self.health.setdefault(league, LeagueHealth(league=league))

    def _record_success(self, league: str, when: datetime, events) -> None:
        health = self._health_for(league)
        health.last_fetch_at = when
        health.last_success_at = when
        health.consecutive_failures = 0
        if events:
            latest = max(event.start for event in events).date()
            if health.last_event_date is None or latest > health.last_event_date:
                health.last_event_date = latest

    def _record_failure(self, league: str, when: datetime) -> None:
        health = self._health_for(league)
        health.last_fetch_at = when
        health.consecutive_failures += 1

    # -- scoreboards ------------------------------------------------------

    def _scoreboard_url(self, league: str, day: date) -> str:
        return "%s/%s/scoreboard?dates=%s" % (SITE_API, league, day.strftime("%Y%m%d"))

    def fetch_day(self, league: str, day: date, now: datetime) -> list:
        """One league, one UTC date, cached.

        Raises FetchError or ParseError. Never returns partial data: a 200 that
        fails validation is a failure, not an empty slate.
        """
        key = (league, day)
        cached = self._scoreboards.get(key)
        if cached is not None and now - cached.fetched_at < self._ttl:
            return cached.events

        payload = self._fetcher.get_json(self._scoreboard_url(league, day))
        events = parse_scoreboard(payload, league)
        self._scoreboards[key] = _CacheEntry(fetched_at=now, events=events)
        return events

    def fetch_window(self, leagues, now: datetime) -> WindowResult:
        """Every league, three UTC days, once per cycle.

        Fetch once per league per cycle into a cache, then assemble every
        subscriber's digest from that cache. A hundred subscribers should still
        cost single-digit API calls, so this is never called per subscriber.
        """
        days = window_dates(now)
        result = WindowResult()

        for league in leagues:
            collected = []
            missing_days = []

            for day in days:
                try:
                    collected.extend(self.fetch_day(league, day, now))
                except (FetchError, ParseError):
                    missing_days.append(day)

            # A missing yesterday or today is a failed league: those two days
            # carry every final and every game the subscriber is reading about,
            # and silently dropping them is the exact failure the three-day
            # window exists to prevent.
            #
            # A missing tomorrow is degraded, not failed. It costs competition
            # subscribers their lookahead and costs team subscribers nothing,
            # which is not worth suppressing a whole digest over.
            critical = [day for day in missing_days if day in days[:2]]

            if critical:
                self._record_failure(league, now)
                result.failed_leagues.add(league)
                continue

            if missing_days:
                result.degraded_leagues.add(league)

            self._record_success(league, now, collected)
            result.events.extend(collected)

        return result

    # -- soccer slugs -----------------------------------------------------

    def soccer_leagues(self, now: datetime) -> dict:
        """Slug -> display name for every soccer competition ESPN knows.

        Served stale indefinitely on error rather than failing: a slug that
        resolved yesterday is almost certainly still right, and this cache
        exists so a soccer outage does not also take out slug resolution.
        """
        fresh = (
            self._slugs is not None
            and self._slugs_fetched_at is not None
            and now - self._slugs_fetched_at < SLUG_TTL
        )
        if fresh:
            return self._slugs

        try:
            payload = self._fetcher.get_json("%s?sport=soccer&limit=500" % DROPDOWN_API)
            leagues = payload["leagues"]
            slugs = {
                entry["slug"]: entry.get("name", entry["slug"])
                for entry in leagues
                if entry.get("slug")
            }
            if not slugs:
                raise FetchError("dropdown returned no slugs")
        except (FetchError, KeyError, TypeError) as exc:
            if self._slugs is not None:
                return self._slugs
            raise FetchError("no soccer slugs available: %s" % exc) from exc

        self._slugs = slugs
        self._slugs_fetched_at = now
        return slugs
