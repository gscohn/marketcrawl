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
    app, db, now = env
    reply = app.test_client().get("/privacy")
    assert reply.headers["Referrer-Policy"] == "no-referrer"
    assert "noindex" in reply.headers["X-Robots-Tag"]
    # No external origins: no CDN, no remote crests. Section 12 promises ESPN
    # sees the server, not the users.
    assert "default-src 'self'" in reply.headers["Content-Security-Policy"]
    assert "img-src 'self'" in reply.headers["Content-Security-Policy"]
