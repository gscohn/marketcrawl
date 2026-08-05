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


def retention(db, windows=(7, 30, 90)) -> list:
    """Of the people who signed up at least N days ago, how many are still here.

    Unrecoverable from current state: a subscriber who deleted leaves no row to
    count, which is why the events table outlives deletion.
    """
    now = datetime.now(timezone.utc)
    out = []
    for days in windows:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cohort = db.execute(
            "SELECT COUNT(DISTINCT subject) FROM events"
            " WHERE event_type = 'signup' AND ts <= ?",
            (cutoff,),
        ).fetchone()[0]
        churned = db.execute(
            "SELECT COUNT(DISTINCT subject) FROM events e"
            " WHERE e.event_type IN ('delete', 'pause')"
            "   AND EXISTS (SELECT 1 FROM events s WHERE s.subject = e.subject"
            "               AND s.event_type = 'signup' AND s.ts <= ?)",
            (cutoff,),
        ).fetchone()[0]
        retained = max(cohort - churned, 0)
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
        "floor": floor,
    }
