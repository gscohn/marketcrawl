"""Config page tests. Spec section 10."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from sportscrawl import repo
from sportscrawl.db import open_database

DID = "did:plc:subscriber"
PEPPER = "test-pepper"


@pytest.fixture
def env(tmp_path):
    path = str(tmp_path / "app.db")
    db = open_database(path)
    now = datetime.now(timezone.utc)
    repo.create_subscriber(db, DID, now, PEPPER)
    db.execute(
        "INSERT INTO teams (league, team_id, display_name, short_name, abbreviation)"
        " VALUES ('baseball/mlb', '19', 'Los Angeles Dodgers', 'Dodgers', 'LAD')"
    )
    app = create_app(path, {"FLASK_SECRET_KEY": "test", "EVENTS_PEPPER": PEPPER,
                            "DATABASE_PATH": path})
    app.config["SESSION_COOKIE_SECURE"] = False
    app.config["TESTING"] = True
    return app, db, now


def mint(db, now, ttl=timedelta(minutes=15), scope="config", did=DID):
    token = secrets.token_urlsafe(16)
    db.execute(
        "INSERT INTO config_tokens (token, did, scope, expires_at) VALUES (?, ?, ?, ?)",
        (token, did, scope, repo.utc_string(now + ttl)),
    )
    return token


def test_a_valid_token_opens_the_settings_page(env):
    app, db, now = env
    token = mint(db, now)
    client = app.test_client()

    reply = client.get("/c/%s" % token, follow_redirects=True)
    assert reply.status_code == 200
    assert b"Your timezone" in reply.data


def test_a_token_survives_a_link_preview_fetch(env):
    """The link is delivered by DM and chat clients fetch URLs to build
    previews. Single use would spend the token before the subscriber tapped
    it, and the failure looks exactly like a broken bot."""
    app, db, now = env
    token = mint(db, now)

    crawler = app.test_client()
    crawler.get("/c/%s" % token)  # preview crawler gets there first

    subscriber = app.test_client()
    reply = subscriber.get("/c/%s" % token, follow_redirects=True)
    assert reply.status_code == 200
    assert b"Your timezone" in reply.data


def test_a_crawler_that_keeps_cookies_does_reach_the_settings_page(env):
    """Documenting the accepted exposure rather than pretending otherwise.

    A crawler that discards cookies gets the generic page. One that keeps them
    across the redirect sees the settings page, and that cannot be prevented
    for a capability URL. It is already the accepted position in section 10:
    the link is no weaker than the DM channel that carried it, and section 12
    says plainly that Bluesky can see DM contents. A Bluesky preview fetch is
    therefore not a new exposure.

    This test exists so nobody later claims a guarantee the design does not
    make.
    """
    app, db, now = env
    token = mint(db, now)

    crawler = app.test_client()  # Flask's client keeps cookies across redirects
    reply = crawler.get("/c/%s" % token, follow_redirects=True)
    assert b"Your timezone" in reply.data


def test_used_at_records_only_the_first_redemption(env):
    app, db, now = env
    token = mint(db, now)
    app.test_client().get("/c/%s" % token)
    first = db.execute(
        "SELECT used_at FROM config_tokens WHERE token = ?", (token,)
    ).fetchone()["used_at"]

    app.test_client().get("/c/%s" % token)
    second = db.execute(
        "SELECT used_at FROM config_tokens WHERE token = ?", (token,)
    ).fetchone()["used_at"]
    assert first == second


def test_an_expired_token_is_refused(env):
    app, db, now = env
    token = mint(db, now, ttl=timedelta(minutes=-1))
    reply = app.test_client().get("/c/%s" % token)
    assert reply.status_code == 404


def test_an_unknown_token_looks_identical_to_an_expired_one(env):
    app, db, now = env
    expired = app.test_client().get("/c/%s" % mint(db, now, ttl=timedelta(minutes=-1)))
    unknown = app.test_client().get("/c/nonsense")
    assert expired.status_code == unknown.status_code == 404
    assert expired.data == unknown.data


def test_settings_requires_a_session(env):
    app, _, _ = env
    reply = app.test_client().get("/settings")
    assert reply.status_code == 403


def test_saving_requires_a_timezone(env):
    """The one field with no safe default. Guessing sends someone a digest at
    3am, so a save without it is refused rather than filled in."""
    app, db, now = env
    client = app.test_client()
    client.get("/c/%s" % mint(db, now))

    with client.session_transaction() as flask_session:
        csrf = flask_session["csrf"] = "tok"

    reply = client.post("/settings", data={"csrf": csrf, "send_time": "07:00"})
    assert reply.status_code == 400


def test_saving_rejects_a_bad_csrf_token(env):
    app, db, now = env
    client = app.test_client()
    client.get("/c/%s" % mint(db, now))

    reply = client.post(
        "/settings",
        data={"csrf": "wrong", "tz": "America/Los_Angeles", "send_time": "07:00"},
    )
    assert reply.status_code == 400


def test_saving_replaces_selections_wholesale(env):
    app, db, now = env
    client = app.test_client()
    client.get("/c/%s" % mint(db, now))
    repo.add_sub(db, DID, "team", "hockey/nhl", "5")

    with client.session_transaction() as flask_session:
        csrf = flask_session["csrf"] = "tok"

    client.post("/settings", data={
        "csrf": csrf,
        "tz": "America/Los_Angeles",
        "send_time": "07:15",
        "team": ["baseball/mlb|19"],
        "competition": ["soccer/fifa.world"],
    })

    subs = repo.subs_for(db, DID)
    assert {(s.kind, s.league, s.team_id) for s in subs} == {
        ("team", "baseball/mlb", "19"),
        ("competition", "soccer/fifa.world", None),
    }
    row = repo.get_subscriber(db, DID)
    assert row["tz"] == "America/Los_Angeles"
    assert row["send_time"] == "07:15"


def test_security_headers_are_present(env):
    """Referrer-Policy and the CSP apply everywhere. X-Robots-Tag does not:
    the landing page and privacy policy are meant to be found, and that is
    asserted separately."""
    app, db, now = env
    reply = app.test_client().get("/privacy")
    assert reply.headers["Referrer-Policy"] == "no-referrer"
    # No external origins: no CDN, no remote crests. Section 12 promises ESPN
    # sees the server, not the users.
    assert "default-src 'self'" in reply.headers["Content-Security-Policy"]
    assert "img-src 'self'" in reply.headers["Content-Security-Policy"]


# --------------------------------------------------------------------------
# Privacy policy, spec section 12
# --------------------------------------------------------------------------


def test_the_privacy_page_makes_every_required_disclosure(env):
    """Each of these is a claim section 12 requires. A policy that drifts from
    the code is worse than none, so the page is asserted rather than trusted."""
    app, _, _ = env
    page = app.test_client().get("/privacy").data.decode("utf-8")

    required = [
        "DID",                      # what is stored
        "timezone",
        "conversation id",          # the transport bookkeeping
        "not anonymous",            # a DID resolves to a public profile
        "not retained",             # inbound DMs
        "never logged",             # message bodies
        "not end-to-end encrypted", # Bluesky can read DMs
        "pseudonymous",             # the surviving counter, named honestly
        "stays in your Bluesky inbox",
        "ESPN sees this server, not you",
        "no uptime guarantee",
        "michael@pusateri.org",     # contact that is not the DM inbox
    ]
    missing = [claim for claim in required if claim not in page]
    assert missing == [], "privacy page is missing: %s" % missing


def test_the_privacy_page_needs_no_session(env):
    """It is linked from the help message, which strangers get first."""
    app, _, _ = env
    assert app.test_client().get("/privacy").status_code == 200


# --------------------------------------------------------------------------
# Admin page, spec section 11
# --------------------------------------------------------------------------


def admin_session(app, db, now):
    token = mint(db, now, scope="admin")
    client = app.test_client()
    client.get("/c/%s" % token)
    return client


def test_admin_requires_authentication(env):
    app, _, _ = env
    reply = app.test_client().get("/admin")
    assert reply.status_code == 401
    assert "Basic" in reply.headers.get("WWW-Authenticate", "")


def test_a_config_session_cannot_reach_admin(env):
    """The scope check has to hold on every handler, not just at exchange."""
    app, db, now = env
    client = app.test_client()
    client.get("/c/%s" % mint(db, now, scope="config"))
    assert client.get("/admin").status_code == 401


def test_an_admin_session_opens_the_page(env):
    app, db, now = env
    reply = admin_session(app, db, now).get("/admin")
    assert reply.status_code == 200
    assert b"Aggregates only" in reply.data


def test_basic_auth_works_when_the_dm_path_is_down(env):
    """The DM link shares a process with the scheduler. If that is wedged, it
    is exactly the morning the dashboard is worth opening."""
    import base64

    path = str(env[0].config["DATABASE_PATH"])
    app = create_app(path, {"FLASK_SECRET_KEY": "t", "EVENTS_PEPPER": PEPPER,
                            "ADMIN_PASSWORD": "hunter2", "DATABASE_PATH": path})
    app.config["SESSION_COOKIE_SECURE"] = False
    header = base64.b64encode(b"admin:hunter2").decode()
    reply = app.test_client().get("/admin", headers={"Authorization": "Basic %s" % header})
    assert reply.status_code == 200


def test_a_wrong_password_is_refused(env):
    import base64

    path = str(env[0].config["DATABASE_PATH"])
    app = create_app(path, {"FLASK_SECRET_KEY": "t", "EVENTS_PEPPER": PEPPER,
                            "ADMIN_PASSWORD": "hunter2", "DATABASE_PATH": path})
    header = base64.b64encode(b"admin:wrong").decode()
    reply = app.test_client().get("/admin", headers={"Authorization": "Basic %s" % header})
    assert reply.status_code == 401


def test_the_admin_page_never_renders_a_did(env):
    """No subscriber list view. If the page cannot show individual rows, the
    privacy claim stays true without relying on discipline."""
    app, db, now = env
    repo.add_sub(db, DID, "team", "baseball/mlb", "19")
    page = admin_session(app, db, now).get("/admin").data.decode("utf-8")
    assert DID not in page
    assert "did:plc:" not in page


def test_small_buckets_are_suppressed(env):
    """A cell reading 1 next to a team nobody else follows identifies a person
    by arithmetic."""
    app, db, now = env
    repo.add_sub(db, DID, "team", "baseball/mlb", "19")
    page = admin_session(app, db, now).get("/admin").data.decode("utf-8")

    assert "Dodgers" in page          # the row exists
    assert "fewer than 5" in page     # but the count is withheld


def test_an_admin_token_lands_on_the_admin_page(env):
    """It created a valid admin session and then redirected to the config
    page, which looked exactly like the admin page not working."""
    app, db, now = env
    client = app.test_client()
    reply = client.get("/c/%s" % mint(db, now, scope="admin"))

    assert reply.status_code == 302
    assert reply.headers["Location"].endswith("/admin")

    page = client.get("/admin")
    assert page.status_code == 200
    assert b"Aggregates only" in page.data


def test_a_config_token_still_lands_on_settings(env):
    app, db, now = env
    reply = app.test_client().get("/c/%s" % mint(db, now, scope="config"))
    assert reply.headers["Location"].endswith("/settings")


def test_the_brand_assets_are_referenced_and_present(env):
    """The logo and favicon are gitignore-adjacent to static/vendor, which was
    excluded once and shipped an unstyled page."""
    import os

    app, _, _ = env
    page = app.test_client().get("/privacy").data.decode("utf-8")

    for asset in ("img/wordmark-navbar.png", "img/favicon-32.png",
                  "img/apple-touch-icon.png"):
        assert asset in page, "%s not referenced in the page" % asset
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", asset)
        assert os.path.exists(path), "%s referenced but missing on disk" % asset


def test_the_navbar_uses_the_brand_navy_not_bootstrap_dark(env):
    app, _, _ = env
    page = app.test_client().get("/privacy").data.decode("utf-8")
    assert "sc-navbar" in page
    assert "bg-dark" not in page


def test_the_root_favicon_path_is_served(env):
    """Browsers probe /favicon.ico whatever the link tags say, and Apache
    proxies everything except /static/ to the app."""
    app, _, _ = env
    reply = app.test_client().get("/favicon.ico")
    assert reply.status_code == 200
    assert reply.data[:4] == b"\x00\x00\x01\x00"  # ICO magic number


# --------------------------------------------------------------------------
# Landing page
# --------------------------------------------------------------------------


def test_the_bare_domain_serves_a_landing_page(env):
    """The Bluesky handle IS this domain, so anyone reading the profile and
    typing it in lands here. It was a bare Flask 404."""
    app, _, _ = env
    reply = app.test_client().get("/")
    assert reply.status_code == 200
    assert b"@sportscrawl.app" in reply.data
    assert b"Privacy" in reply.data or b"privacy" in reply.data


def test_the_landing_page_and_privacy_are_indexable(env):
    """They are meant to be found. The bio points at the domain."""
    app, _, _ = env
    client = app.test_client()
    for path in ("/", "/privacy"):
        reply = client.get(path)
        assert "X-Robots-Tag" not in reply.headers, path
        assert b'name="robots"' not in reply.data, path


def test_capability_and_operator_routes_stay_noindex(env):
    """Anything carrying a token, or belonging to the operator, stays out."""
    app, db, now = env
    client = app.test_client()
    client.get("/c/%s" % mint(db, now))

    for path in ("/settings", "/admin", "/c/whatever"):
        reply = client.get(path)
        assert "noindex" in reply.headers.get("X-Robots-Tag", ""), path


def test_no_em_dashes_in_any_rendered_page(env):
    """A house style rule, asserted so it does not drift back in."""
    app, db, now = env
    client = app.test_client()
    client.get("/c/%s" % mint(db, now, scope="admin"))

    for path in ("/", "/privacy", "/admin"):
        page = client.get(path).data.decode("utf-8")
        assert "—" not in page, "em dash in %s" % path
        assert "&mdash;" not in page, "mdash entity in %s" % path

    settings_client = app.test_client()
    settings_client.get("/c/%s" % mint(db, now))
    page = settings_client.get("/settings").data.decode("utf-8")
    assert "—" not in page and "&mdash;" not in page


def test_the_front_page_leads_with_the_full_wordmark(env):
    """The only page a stranger sees before deciding, so it gets the logo and
    wordmark rather than the small navbar mark."""
    app, _, _ = env
    page = app.test_client().get("/").data.decode("utf-8")
    assert "sportscrawl-wordmark-horizontal.png" in page
    assert "sc-navbar" not in page          # navbar suppressed here
    assert "wordmark-navbar.png" not in page


def test_the_footer_links_to_the_source_repository(env):
    """On every page, since the footer is in the base template."""
    app, db, now = env
    client = app.test_client()

    for path in ("/", "/privacy"):
        page = client.get(path).data.decode("utf-8")
        assert "Source Code:" in page, path
        assert "https://github.com/cruftbox/sportscrawl" in page, path
