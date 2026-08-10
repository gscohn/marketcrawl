"""Seed the teams table from ESPN.

Source for the canonical team_id used as the subscription key, the alias
fields used for command matching, and the crests on the config page.
Spec section 5.

    python seed_teams.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from sportscrawl.config import settings
from sportscrawl.db import open_database
from sportscrawl.http import Fetcher
from sportscrawl.source import SITE_API, US_PRO_LEAGUES

# Only leagues with a stable roster of clubs get seeded. Tournament slugs
# (World Cup, Champions League) field national teams and rotating entrants,
# and are subscribed to as competitions rather than picked team by team.
SEEDED_LEAGUES = list(US_PRO_LEAGUES) + [
    "soccer/usa.1",
    "soccer/usa.nwsl",
    "soccer/eng.1",
]


# A roster this much smaller than what is already active is treated as a bad
# response rather than a mass departure. Three clubs go down from the Premier
# League each May, which is 15% of twenty, so the guard has to sit below that
# while still catching a truncated or half-empty payload.
SHRINK_GUARD = 0.7


def team_rows(payload, league, seen_at):
    """ESPN nests these four deep: sports[0].leagues[0].teams[].team."""
    sports = payload.get("sports") or []
    leagues = (sports[0].get("leagues") if sports else []) or []
    entries = (leagues[0].get("teams") if leagues else []) or []

    for entry in entries:
        team = entry.get("team") or {}
        if not team.get("id"):
            continue
        logos = team.get("logos") or []
        yield (
            league,
            str(team["id"]),
            team.get("displayName"),
            team.get("shortDisplayName"),
            team.get("nickname"),
            team.get("location"),
            team.get("abbreviation"),
            logos[0].get("href") if logos else None,
            seen_at,
        )


CREST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "crests")


def _image_path(logo_url: str) -> str:
    """The combiner takes a site-relative path, not a full URL."""
    marker = "/i/teamlogos/"
    index = logo_url.find(marker)
    return logo_url[index:] if index >= 0 else logo_url


def download_crests(connection, directory=CREST_DIR) -> int:
    """Copy every team crest onto our own host.

    The config page must not load images from a.espncdn.com. Section 12
    promises "ESPN sees the server, not the users", and a remote <img> hands
    every visitor's IP to ESPN's CDN, which would make that claim false. Same
    reasoning that keeps Bootstrap out of a CDN.

    Crests are refetchable, so they are gitignored rather than committed;
    deploy.sh runs this.
    """
    import requests

    saved = 0
    for row in connection.execute(
        "SELECT league, team_id, logo_url FROM teams WHERE logo_url IS NOT NULL"
    ).fetchall():
        league_dir = os.path.join(directory, row["league"].replace("/", "_"))
        os.makedirs(league_dir, exist_ok=True)
        target = os.path.join(league_dir, "%s.png" % row["team_id"])
        if os.path.exists(target):
            continue
        try:
            # Crests ship at 500px and get displayed at 28. ESPN's combiner
            # resizes on their side: ~3.5KB instead of ~50KB, across every
            # league. Over 205 teams that is 0.7MB against 12.3MB, which on a
            # phone is the difference between a usable picker and a data bill.
            #
            # The per-size path (/100/ instead of /500/) only exists for MLB;
            # everything else 404s. The combiner is the one that works
            # everywhere, so it is not worth trying both.
            reply = requests.get(
                "https://a.espncdn.com/combiner/i",
                params={"img": _image_path(row["logo_url"]), "w": 96, "h": 96},
                timeout=(5, 15),
            )
            if reply.status_code != 200:
                reply = requests.get(row["logo_url"], timeout=(5, 15))
            if reply.status_code == 200:
                with open(target, "wb") as handle:
                    handle.write(reply.content)
                saved += 1
        except Exception:
            # A missing crest is a cosmetic problem. The picker falls back to
            # the abbreviation, so it must not take the seed run down.
            continue
    return saved


def local_crest(league: str, team_id: str) -> str:
    return "crests/%s/%s.png" % (league.replace("/", "_"), team_id)


def seed(connection, fetcher=None, leagues=SEEDED_LEAGUES, now=None) -> dict:
    """Refresh every seeded league and retire clubs that have left one.

    Safe to run repeatedly; it is meant to, on a weekly timer. Teams that
    reappear are reactivated, so promotion the season after relegation needs no
    intervention.
    """
    fetcher = fetcher or Fetcher()
    seen_at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = {}

    for league in leagues:
        before = connection.execute(
            "SELECT COUNT(*) FROM teams WHERE league = ? AND active = 1", (league,)
        ).fetchone()[0]

        payload = fetcher.get_json("%s/%s/teams" % (SITE_API, league))
        rows = list(team_rows(payload, league, seen_at))

        connection.executemany(
            "INSERT INTO teams"
            " (league, team_id, display_name, short_name, nickname, location,"
            "  abbreviation, logo_url, last_seen_at, active)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)"
            " ON CONFLICT(league, team_id) DO UPDATE SET"
            "  display_name = excluded.display_name,"
            "  short_name = excluded.short_name,"
            "  nickname = excluded.nickname,"
            "  location = excluded.location,"
            "  abbreviation = excluded.abbreviation,"
            "  logo_url = excluded.logo_url,"
            "  last_seen_at = excluded.last_seen_at,"
            "  active = 1",
            rows,
        )

        # Never retire anything on the strength of a suspiciously thin
        # response. A 200 carrying half a roster is a failure mode of this API,
        # and the cost of acting on one is deactivating clubs people follow.
        retired = 0
        if not rows or (before and len(rows) < before * SHRINK_GUARD):
            guarded = True
        else:
            guarded = False
            cursor = connection.execute(
                "UPDATE teams SET active = 0"
                " WHERE league = ? AND active = 1"
                "   AND (last_seen_at IS NULL OR last_seen_at < ?)",
                (league, seen_at),
            )
            retired = cursor.rowcount or 0

        report[league] = {"seen": len(rows), "retired": retired, "guarded": guarded}

    return report


def main(argv=None) -> int:
    config = settings()
    path = config.get("DATABASE_PATH") or os.environ.get("DATABASE_PATH", "sportscrawl.db")
    connection = open_database(path)
    report = seed(connection)
    saved = download_crests(connection)
    print("crests downloaded: %d" % saved)
    for league, info in report.items():
        note = ""
        if info["guarded"]:
            note = "  SHORT RESPONSE, nothing retired"
        elif info["retired"]:
            note = "  retired %d" % info["retired"]
        print("%-22s %3d teams%s" % (league, info["seen"], note))
    print("total %d, database %s" % (sum(i["seen"] for i in report.values()), path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
