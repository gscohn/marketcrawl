"""Team roster lifecycle. The teams table was frozen at first seed, so a club
that left its league kept its row, stayed selectable, and produced silence
indistinguishable from the intentional empty-digest rule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import seed_teams
from sportscrawl.db import connect, migrate

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
EPL = "soccer/eng.1"


@pytest.fixture
def db(tmp_path):
    connection = connect(str(tmp_path / "seed.db"))
    migrate(connection)
    return connection


class FakeFetcher:
    """Returns whatever roster the test sets, in ESPN's nesting."""

    def __init__(self, rosters):
        self.rosters = rosters

    def get_json(self, url):
        league = url.rsplit("/teams", 1)[0].split("/sports/", 1)[1]
        teams = [
            {"team": {"id": tid, "displayName": name, "shortDisplayName": name,
                      "abbreviation": name[:3].upper(), "logos": []}}
            for tid, name in self.rosters.get(league, [])
        ]
        return {"sports": [{"leagues": [{"teams": teams}]}]}


def roster(*names):
    """Ids derived from the name, so a promoted club cannot silently collide
    with an existing one. An earlier version numbered positionally and quietly
    renamed three incumbents instead of adding three clubs."""
    return [(str(abs(hash(n)) % 100000), n) for n in names]


def active(db, league=EPL):
    return {
        r["short_name"] for r in db.execute(
            "SELECT short_name FROM teams WHERE league = ? AND active = 1", (league,)
        )
    }


def test_a_club_that_leaves_is_retired_not_deleted(db):
    """Deactivated so an existing subscription still resolves to a name."""
    twenty = roster(*["Club%02d" % i for i in range(20)])
    seed_teams.seed(db, FakeFetcher({EPL: twenty}), leagues=[EPL], now=NOW)
    assert len(active(db)) == 20

    # Three go down, three come up: a Premier League May.
    next_season = twenty[:17] + roster("Up1", "Up2", "Up3")
    seed_teams.seed(db, FakeFetcher({EPL: next_season}), leagues=[EPL],
                    now=NOW + timedelta(days=280))

    still_active = active(db)
    assert len(still_active) == 20
    assert "Club17" not in still_active            # relegated
    row = db.execute(
        "SELECT active FROM teams WHERE short_name = 'Club17'"
    ).fetchone()
    assert row is not None and row["active"] == 0  # row kept, name resolvable


def test_a_promoted_club_appears(db):
    """The frozen table meant a new subscriber could not find their club."""
    seed_teams.seed(db, FakeFetcher({EPL: roster("A", "B")}), leagues=[EPL], now=NOW)
    seed_teams.seed(db, FakeFetcher({EPL: roster("A", "B", "C")}), leagues=[EPL],
                    now=NOW + timedelta(days=7))
    assert "C" in active(db)


def test_a_returning_club_is_reactivated(db):
    """Promotion the season after relegation needs no intervention.

    Uses a full-sized league: dropping one of three would be a 33% shrink and
    would rightly trip the guard, which is a fact about the test, not the code.
    """
    full = roster(*["Club%02d" % i for i in range(20)])
    seed_teams.seed(db, FakeFetcher({EPL: full}), leagues=[EPL], now=NOW)

    seed_teams.seed(db, FakeFetcher({EPL: full[:19]}), leagues=[EPL],
                    now=NOW + timedelta(days=7))
    assert "Club19" not in active(db)

    seed_teams.seed(db, FakeFetcher({EPL: full}), leagues=[EPL],
                    now=NOW + timedelta(days=14))
    assert "Club19" in active(db)


def test_a_rename_updates_in_place(db):
    """Firstlight's actual trigger. The id is the key, so the name follows."""
    seed_teams.seed(db, FakeFetcher({EPL: [("1", "Old Name")]}), leagues=[EPL], now=NOW)
    seed_teams.seed(db, FakeFetcher({EPL: [("1", "New Name")]}), leagues=[EPL],
                    now=NOW + timedelta(days=7))
    assert active(db) == {"New Name"}
    assert db.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 1


def test_a_truncated_response_retires_nobody(db):
    """A 200 carrying half a roster is a failure mode of this API. Acting on
    one would deactivate clubs people follow."""
    twenty = roster(*["Club%02d" % i for i in range(20)])
    seed_teams.seed(db, FakeFetcher({EPL: twenty}), leagues=[EPL], now=NOW)

    report = seed_teams.seed(db, FakeFetcher({EPL: twenty[:5]}), leagues=[EPL],
                             now=NOW + timedelta(days=7))
    assert report[EPL]["guarded"] is True
    assert report[EPL]["retired"] == 0
    assert len(active(db)) == 20


def test_an_empty_response_retires_nobody(db):
    seed_teams.seed(db, FakeFetcher({EPL: roster("A", "B")}), leagues=[EPL], now=NOW)
    report = seed_teams.seed(db, FakeFetcher({EPL: []}), leagues=[EPL],
                             now=NOW + timedelta(days=7))
    assert report[EPL]["guarded"] is True
    assert len(active(db)) == 2


def test_relegation_is_below_the_guard(db):
    """Three of twenty is 15%, so a real May must still be actioned."""
    twenty = roster(*["Club%02d" % i for i in range(20)])
    seed_teams.seed(db, FakeFetcher({EPL: twenty}), leagues=[EPL], now=NOW)
    report = seed_teams.seed(db, FakeFetcher({EPL: twenty[:17]}), leagues=[EPL],
                             now=NOW + timedelta(days=280))
    assert report[EPL]["guarded"] is False
    assert report[EPL]["retired"] == 3
