"""Aggregate statistics for the admin page.

Aggregates only. There is no subscriber list view, and no function here
returns a DID. If the page cannot display individual rows, the privacy claim
stays true without relying on discipline. Spec section 11.

Four of these metrics cannot be computed from current state at all, because
deletion removes the row. They come from the append-only events table, which
is the one thing `delete` does not touch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Any bucket smaller than this is reported as suppressed rather than shown.
# A cell reading "1" next to a team nobody else follows identifies a person by
# arithmetic, which is exactly what an aggregate-only page is meant to prevent.
MIN_BUCKET = 5


def _suppress(count: int, floor: int) -> object:
    return count if count >= floor else None


def state_counts(db) -> dict:
    """Active and paused come from current state; deleted only exists in
    events, because the subscriber row is gone."""
    rows = dict(
        db.execute("SELECT state, COUNT(*) FROM subscribers GROUP BY state").fetchall()
    )
    deleted = db.execute(
        "SELECT COUNT(DISTINCT subject) FROM events WHERE event_type = 'delete'"
    ).fetchone()[0]
    return {
        "active": rows.get("active", 0),
        "paused": rows.get("paused", 0),
        "deleted": deleted,
    }


def signups_over_time(db, days=30) -> list:
    """Cumulative subscriber count by day: signups minus deletes."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT substr(ts, 1, 10) AS day,"
        " SUM(CASE WHEN event_type = 'signup' THEN 1 ELSE 0 END) AS joined,"
        " SUM(CASE WHEN event_type = 'delete' THEN 1 ELSE 0 END) AS left_"
        " FROM events WHERE ts >= ? AND event_type IN ('signup', 'delete')"
        " GROUP BY day ORDER BY day",
        (since,),
    ).fetchall()

    series = []
    running = 0
    for row in rows:
        running += row["joined"] - row["left_"]
        series.append({
            "day": row["day"],
            "joined": row["joined"],
            "left": row["left_"],
            "total": running,
        })
    return series


def selections_by_league(db, floor=MIN_BUCKET) -> list:
    rows = db.execute(
        "SELECT league, COUNT(DISTINCT did) AS n FROM subs"
        " GROUP BY league ORDER BY n DESC, league"
    ).fetchall()
    return [{"league": r["league"], "count": _suppress(r["n"], floor)} for r in rows]


def selections_by_team(db, limit=25, floor=MIN_BUCKET) -> list:
    rows = db.execute(
        "SELECT s.league, s.team_id, COALESCE(t.short_name, t.display_name, s.team_id) AS name,"
        "       COUNT(DISTINCT s.did) AS n"
        " FROM subs s LEFT JOIN teams t"
        "   ON t.league = s.league AND t.team_id = s.team_id"
        " WHERE s.kind = 'team'"
        " GROUP BY s.league, s.team_id ORDER BY n DESC, name LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"league": r["league"], "name": r["name"], "count": _suppress(r["n"], floor)}
        for r in rows
    ]


def delivery_times(db, floor=MIN_BUCKET) -> list:
    rows = db.execute(
        "SELECT send_time, COUNT(*) AS n FROM subscribers"
        " WHERE tz IS NOT NULL GROUP BY send_time ORDER BY send_time"
    ).fetchall()
    return [{"time": r["send_time"], "count": _suppress(r["n"], floor)} for r in rows]


def retention(db, windows=(7, 30, 90), now=None) -> list:
    """Of the people who signed up at least N days ago, how many were still
    subscribed N days after their own signup.

    Cohort retention, anchored to each subject's signup rather than to today.
    An earlier version counted any pause or delete at any time, with no window
    and no check for a later resume, so somebody who paused in February and
    came back in March was churned forever, somebody who signed up sixty days
    ago and paused yesterday counted against the 7 day figure, and all three
    windows converged on the same number.

    Computed in Python rather than SQL. The events table is small, and the
    "was this subject paused at a given instant" question needs the pause and
    resume history in order, which is unreadable as a correlated subquery.
    """
    now = now or datetime.now(timezone.utc)

    history = {}
    for row in db.execute(
        "SELECT subject, event_type, ts FROM events"
        " WHERE event_type IN ('signup', 'pause', 'resume', 'delete')"
        " ORDER BY ts, id"
    ):
        history.setdefault(row["subject"], []).append((row["ts"], row["event_type"]))

    out = []
    for days in windows:
        cohort = 0
        retained = 0
        horizon = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for events in history.values():
            signup = next((ts for ts, kind in events if kind == "signup"), None)
            if signup is None or signup > horizon:
                continue          # joined too recently to have reached day N
            cohort += 1

            anniversary = (
                datetime.strptime(signup, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc) + timedelta(days=days)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

            # Replay their history up to their own day N. Paused counts as
            # churned only if they had not resumed by then.
            subscribed = False
            for ts, kind in events:
                if ts > anniversary:
                    break
                if kind == "signup" or kind == "resume":
                    subscribed = True
                elif kind == "pause":
                    subscribed = False
                elif kind == "delete":
                    subscribed = False
            if subscribed:
                retained += 1

        out.append({
            "days": days,
            "cohort": cohort,
            "retained": retained,
            "pct": round(100.0 * retained / cohort, 1) if cohort else None,
        })
    return out


def stop_stats(db) -> dict:
    """Paused and deleted are different signals and are counted separately.

    Paused suggests seasonal quiet, deleted suggests churn. That distinction is
    the metric the project exists to gather.
    """
    signups = db.execute(
        "SELECT COUNT(DISTINCT subject) FROM events WHERE event_type = 'signup'"
    ).fetchone()[0]
    paused = db.execute(
        "SELECT COUNT(DISTINCT subject) FROM events WHERE event_type = 'pause'"
    ).fetchone()[0]
    deleted = db.execute(
        "SELECT COUNT(DISTINCT subject) FROM events WHERE event_type = 'delete'"
    ).fetchone()[0]

    gaps = db.execute(
        "SELECT (julianday(MIN(p.ts)) - julianday(MIN(s.ts))) AS days"
        " FROM events s JOIN events p ON p.subject = s.subject"
        " WHERE s.event_type = 'signup' AND p.event_type IN ('pause', 'delete')"
        " GROUP BY s.subject"
    ).fetchall()
    values = sorted(g["days"] for g in gaps if g["days"] is not None)
    median = values[len(values) // 2] if values else None

    return {
        "signups": signups,
        "paused": paused,
        "deleted": deleted,
        "stop_rate": round(100.0 * (paused + deleted) / signups, 1) if signups else None,
        "median_days_to_stop": round(median, 1) if median is not None else None,
    }


def config_visits(db) -> dict:
    loads = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'config_load'"
    ).fetchone()[0]
    saves = db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = 'config_save'"
    ).fetchone()[0]
    returners = db.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT subject FROM events WHERE event_type = 'config_load'"
        "  GROUP BY subject HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    return {"loads": loads, "saves": saves, "returning": returners}


def orphaned_subs(db) -> list:
    """Team subscriptions whose club is no longer in that league.

    The roster is the authority on whether a team still exists in a league, so
    this is deterministic. The obvious alternative, warning on subscriptions
    that produced no line for N days, cannot work: an NFL-only subscriber
    legitimately matches nothing from February to September, and a team on a
    bye matches nothing for a week. Absence of games says nothing; absence
    from the roster says everything.
    """
    rows = db.execute(
        "SELECT s.league, s.team_id,"
        "       COALESCE(t.short_name, t.display_name, s.team_id) AS name,"
        "       COUNT(DISTINCT s.did) AS subscribers,"
        "       CASE WHEN t.league IS NULL THEN 'not in table'"
        "            ELSE 'left the league' END AS reason"
        " FROM subs s"
        " LEFT JOIN teams t ON t.league = s.league AND t.team_id = s.team_id"
        " WHERE s.kind = 'team' AND (t.league IS NULL OR t.active = 0)"
        " GROUP BY s.league, s.team_id"
        " ORDER BY subscribers DESC, name"
    ).fetchall()
    return [dict(row) for row in rows]


def league_health(db) -> list:
    return [
        dict(row)
        for row in db.execute(
            "SELECT league, last_success_at, last_event_date, consecutive_failures"
            " FROM league_state ORDER BY consecutive_failures DESC, league"
        ).fetchall()
    ]


def summary(db, floor=MIN_BUCKET) -> dict:
    return {
        "states": state_counts(db),
        "timeline": signups_over_time(db),
        "leagues": selections_by_league(db, floor),
        "teams": selections_by_team(db, floor=floor),
        "delivery": delivery_times(db, floor),
        "retention": retention(db),
        "stops": stop_stats(db),
        "config": config_visits(db),
        "health": league_health(db),
        "orphans": orphaned_subs(db),
        "floor": floor,
    }
