"""Config page, privacy policy, and admin.

Server-rendered Flask with Jinja templates. No JS framework. The scheduler
does NOT run here: gunicorn runs multiple workers and each would fire the same
job. Spec section 3.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from flask import (
    Flask,
    Response,
    abort,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from sportscrawl import repo, stats
from sportscrawl.config import settings
from sportscrawl.db import open_database

TOKEN_TTL = timedelta(minutes=15)
CONFIG_SESSION_TTL = timedelta(days=30)
ADMIN_SESSION_TTL = timedelta(hours=24)

# Competitions are subscribed to whole rather than team by team: they field
# national sides and rotating entrants. Spec section 4.
COMPETITIONS = [
    ("soccer/fifa.world", "FIFA World Cup"),
    ("soccer/uefa.champions", "UEFA Champions League"),
    ("soccer/concacaf.champions", "Concacaf Champions Cup"),
    ("soccer/usa.1", "MLS (whole slate)"),
    ("soccer/usa.nwsl", "NWSL (whole slate)"),
]

LEAGUE_LABELS = {
    "football/nfl": "NFL",
    "basketball/nba": "NBA",
    "basketball/wnba": "WNBA",
    "baseball/mlb": "MLB",
    "hockey/nhl": "NHL",
    "soccer/usa.1": "MLS",
    "soccer/usa.nwsl": "NWSL",
    "soccer/eng.1": "Premier League",
}

# A curated list beats 600 IANA names in a dropdown. US zones first, since
# that is where the leagues are, then the common international ones.
TIMEZONES = [
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "America/Mexico_City",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Lisbon",
    "Australia/Sydney",
    "Australia/Melbourne",
    "Pacific/Auckland",
    "Asia/Tokyo",
    "Asia/Singapore",
    "UTC",
]


def delivery_times():
    """Quarter hours. Round numbers are what people pick anyway, and it keeps
    the pacing queue in section 7 predictable."""
    for hour in range(24):
        for minute in (0, 15, 30, 45):
            label = "%d:%02d%s" % (hour % 12 or 12, minute, "am" if hour < 12 else "pm")
            yield "%02d:%02d" % (hour, minute), label


def create_app(database_path=None, config=None):
    config = config or settings()
    app = Flask(__name__)
    app.secret_key = config.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
    app.config["DATABASE_PATH"] = (
        database_path
        or config.get("DATABASE_PATH")
        or os.environ.get("DATABASE_PATH", "sportscrawl.db")
    )
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    def db():
        if "db" not in g:
            g.db = open_database(app.config["DATABASE_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exception):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.after_request
    def headers(response):
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # No external origins at all: no CDN, no remote crests, no fonts.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    # -- authentication ---------------------------------------------------

    def current_did():
        sid = session.get("sid")
        if not sid:
            return None
        row = db().execute(
            "SELECT did, scope, expires_at FROM sessions WHERE sid = ?", (sid,)
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < repo.utc_string(datetime.now(timezone.utc)):
            return None
        return row["did"]

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.route("/c/<token>")
    def redeem(token):
        """Exchange a capability URL for a session.

        Redeemable repeatedly until it expires, NOT single use. The link
        arrives in a Bluesky DM and chat clients fetch URLs to build link
        previews, so a single-use token would routinely be spent by a preview
        crawler before the subscriber ever tapped it. The same happens with a
        browser prefetch, a double click, or a reload.

        Security still rests on the token being unguessable and short lived;
        `used_at` records the first redemption for the metrics in section 11
        without invalidating anything. A wrong or expired token gets one
        identical page: never distinguish the cases.
        """
        now = datetime.now(timezone.utc)
        row = db().execute(
            "SELECT * FROM config_tokens WHERE token = ?", (token,)
        ).fetchone()

        if row is None or row["expires_at"] < repo.utc_string(now):
            return render_template("expired.html"), 404

        ttl = ADMIN_SESSION_TTL if row["scope"] == "admin" else CONFIG_SESSION_TTL
        sid = secrets.token_urlsafe(32)
        db().execute(
            "INSERT INTO sessions (sid, did, scope, created_at, expires_at, last_seen_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                sid,
                row["did"],
                row["scope"],
                repo.utc_string(now),
                repo.utc_string(now + ttl),
                repo.utc_string(now),
            ),
        )
        db().execute(
            "UPDATE config_tokens SET used_at = COALESCE(used_at, ?) WHERE token = ?",
            (repo.utc_string(now), token),
        )
        session["sid"] = sid
        repo.record_event(
            db(), now, "config_load", row["did"], config.get("EVENTS_PEPPER", "dev")
        )
        return redirect(url_for("config_page"))

    # -- config page ------------------------------------------------------

    @app.route("/settings")
    def config_page():
        did = current_did()
        if not did:
            return render_template("expired.html"), 403

        subscriber = repo.get_subscriber(db(), did)
        chosen = repo.subs_for(db(), did)
        chosen_teams = {(s.league, s.team_id) for s in chosen if s.kind == "team"}
        chosen_comps = {s.league for s in chosen if s.kind == "competition"}

        rows = db().execute(
            "SELECT * FROM teams ORDER BY league, display_name"
        ).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault(row["league"], []).append(row)

        # Leagues collapse on a phone, so each one needs to say what is inside
        # it without being opened, and a league the subscriber has already
        # picked from must not hide those picks.
        selected_counts = {
            league: sum(1 for (lg, _) in chosen_teams if lg == league)
            for league in grouped
        }

        return render_template(
            "settings.html",
            subscriber=subscriber,
            grouped=grouped,
            league_labels=LEAGUE_LABELS,
            competitions=COMPETITIONS,
            chosen_teams=chosen_teams,
            chosen_comps=chosen_comps,
            selected_counts=selected_counts,
            timezones=TIMEZONES,
            times=list(delivery_times()),
            saved=request.args.get("saved"),
        )

    @app.route("/settings", methods=["POST"])
    def save_settings():
        did = current_did()
        if not did:
            return render_template("expired.html"), 403
        if request.form.get("csrf") != session.get("csrf"):
            abort(400)

        tz = request.form.get("tz") or ""
        if tz not in TIMEZONES:
            # The one field with no safe default. Guessing sends someone their
            # digest at 3am, so a save without it is rejected rather than
            # filled in. Spec section 10.
            abort(400)

        send_time = request.form.get("send_time") or "07:00"
        now = datetime.now(timezone.utc)

        db().execute("BEGIN")
        try:
            db().execute(
                "UPDATE subscribers SET tz = ?, send_time = ? WHERE did = ?",
                (tz, send_time, did),
            )
            db().execute("DELETE FROM subs WHERE did = ?", (did,))
            for value in request.form.getlist("team"):
                league, team_id = value.rsplit("|", 1)
                repo.add_sub(db(), did, "team", league, team_id)
            for league in request.form.getlist("competition"):
                repo.add_sub(db(), did, "competition", league)
            repo.record_event(
                db(), now, "config_save", did, config.get("EVENTS_PEPPER", "dev")
            )
            db().execute("COMMIT")
        except Exception:
            db().execute("ROLLBACK")
            raise

        return redirect(url_for("config_page", saved=1))

    # -- admin ------------------------------------------------------------

    def admin_ok():
        """Two paths, deliberately. Spec section 11.

        The DM link is the one that works from a phone; basic auth is the one
        that works when the poller is wedged, which is exactly the morning the
        dashboard is worth opening.
        """
        sid = session.get("sid")
        if sid:
            row = db().execute(
                "SELECT scope, expires_at FROM sessions WHERE sid = ?", (sid,)
            ).fetchone()
            if (
                row is not None
                and row["scope"] == "admin"
                and row["expires_at"] >= repo.utc_string(datetime.now(timezone.utc))
            ):
                return True

        secret = config.get("ADMIN_PASSWORD")
        auth = request.authorization
        if secret and auth and auth.password and secrets.compare_digest(
            auth.password, secret
        ):
            return True
        return False

    @app.route("/admin")
    def admin():
        if not admin_ok():
            # Ask for basic auth rather than 403: the password path has to be
            # reachable when the DM path is down.
            return Response(
                "", 401, {"WWW-Authenticate": 'Basic realm="sportscrawl"'}
            )

        floor = int(config.get("ADMIN_MIN_BUCKET") or stats.MIN_BUCKET)
        return render_template("admin.html", data=stats.summary(db(), floor=floor))

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html")

    return app


app = create_app()


if __name__ == "__main__":
    # Development only. Production is gunicorn on loopback behind Apache.
    app.config["SESSION_COOKIE_SECURE"] = False
    app.run(host="127.0.0.1", port=5000, debug=True)
