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
    send_from_directory,
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
from sportscrawl.db import connect, open_database
from sportscrawl.logs import configure

TOKEN_TTL = timedelta(minutes=15)

# Spec section 10 asks for the capability route to be rate limited.
RATE_LIMIT = 20
RATE_WINDOW = timedelta(minutes=5)
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
    configure()
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

    # Migrations run once here, not per request. open_database() migrates, and
    # calling it from db() meant every page load recreated schema_version,
    # queried it and globbed migrations/ -- and let a web worker race the
    # scheduler applying a migration during a deploy.
    open_database(app.config["DATABASE_PATH"]).close()

    def db():
        if "db" not in g:
            g.db = connect(app.config["DATABASE_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exception):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    # The landing page and the privacy policy are meant to be found. Everything
    # else either carries a capability token or belongs to the operator.
    PUBLIC_ENDPOINTS = {"home", "privacy", "static", "favicon"}

    @app.after_request
    def headers(response):
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.endpoint not in PUBLIC_ENDPOINTS:
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # No external origins at all: no CDN, no remote crests, no fonts.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        )
        return response

    # Per-process, which is per gunicorn worker, so the real ceiling is this
    # times the worker count. Good enough: a 32 byte token is not guessable and
    # this exists to stop the route being a free oracle, not to be exact.
    redeem_hits = {}

    def rate_limited(key, now, limit=RATE_LIMIT, window=RATE_WINDOW) -> bool:
        cutoff = now - window
        hits = [t for t in redeem_hits.get(key, []) if t > cutoff]
        hits.append(now)
        redeem_hits[key] = hits
        if len(redeem_hits) > 2048:            # crude bound on memory
            for stale in [k for k, v in redeem_hits.items() if not v or v[-1] < cutoff]:
                redeem_hits.pop(stale, None)
        return len(hits) > limit

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
        if rate_limited(request.remote_addr or "unknown", now):
            return render_template("expired.html"), 429

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
        # Land on the page the token was issued for. Redirecting everything to
        # the config page made the admin link create a valid admin session and
        # then drop the operator on the wrong screen.
        if row["scope"] == "admin":
            return redirect(url_for("admin"))
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

        # Active only. A club that has left its league stays in the table so
        # existing subscriptions still resolve to a readable name, but offering
        # it to somebody new would be selling a guaranteed silence.
        rows = db().execute(
            "SELECT * FROM teams WHERE active = 1 ORDER BY league, display_name"
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
        submitted = request.form.get("csrf") or ""
        expected = session.get("csrf") or ""
        if not secrets.compare_digest(submitted, expected):
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
            # The scheduler sends the confirmation; see repo.queue_confirmation.
            repo.queue_confirmation(db(), did, now)
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

        try:
            floor = int(config.get("ADMIN_MIN_BUCKET") or stats.MIN_BUCKET)
        except (TypeError, ValueError):
            # A typo in the env file must not take the page down, and it must
            # never fail open to a smaller floor.
            floor = stats.MIN_BUCKET
        return render_template("admin.html", data=stats.summary(db(), floor=floor))

    @app.route("/")
    def home():
        """The bare domain. The Bluesky handle IS this domain, so anyone who
        reads the profile and types it in lands here; before this route
        existed they got a bare Flask 404."""
        return render_template("index.html")

    @app.route("/favicon.ico")
    def favicon():
        """Browsers probe this path whatever the link tags say, and Apache
        proxies everything except /static/ here, so without a route it is a
        404 in the log on every first visit."""
        return send_from_directory(
            os.path.join(app.root_path, "static", "img"),
            "favicon.ico",
            mimetype="image/vnd.microsoft.icon",
        )

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html")

    return app


app = create_app()


if __name__ == "__main__":
    # Development only. Production is gunicorn on loopback behind Apache.
    app.config["SESSION_COOKIE_SECURE"] = False
    app.run(host="127.0.0.1", port=5000, debug=True)
