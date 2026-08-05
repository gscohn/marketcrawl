-- Initial schema. Spec section 4.
--
-- Every migration must be safe to run twice: IF NOT EXISTS everywhere, no
-- destructive statements. The database persists across deploys.
--
-- Targets SQLite 3.26 (AlmaLinux 8 system library, 2018): no STRICT tables,
-- no RETURNING. Spec section 13.

CREATE TABLE IF NOT EXISTS subscribers (
    did                 TEXT PRIMARY KEY,
    tz                  TEXT,           -- null until the config page is saved
    send_time           TEXT NOT NULL DEFAULT '07:00',
    state               TEXT NOT NULL DEFAULT 'active',
    created_at          TEXT NOT NULL,
    paused_at           TEXT,
    last_sent_date      TEXT,
    last_failure_date   TEXT,
    last_evaluated_date TEXT
);

CREATE TABLE IF NOT EXISTS subs (
    id      INTEGER PRIMARY KEY,
    did     TEXT NOT NULL REFERENCES subscribers(did) ON DELETE CASCADE,
    kind    TEXT NOT NULL,              -- 'team' or 'competition'
    league  TEXT NOT NULL,
    team_id TEXT
);

-- Guards against a partial save leaving a duplicate that renders the same
-- game twice. COALESCE because SQLite treats NULLs as distinct in a UNIQUE
-- index, which would let competition rows duplicate freely.
CREATE UNIQUE INDEX IF NOT EXISTS subs_unique
    ON subs (did, kind, league, COALESCE(team_id, ''));

CREATE INDEX IF NOT EXISTS subs_did ON subs (did);

CREATE TABLE IF NOT EXISTS teams (
    league       TEXT NOT NULL,
    team_id      TEXT NOT NULL,
    display_name TEXT,
    short_name   TEXT,
    nickname     TEXT,
    location     TEXT,
    abbreviation TEXT,
    logo_url     TEXT,
    PRIMARY KEY (league, team_id)       -- ESPN ids are unique per league only
);

CREATE TABLE IF NOT EXISTS config_tokens (
    token      TEXT PRIMARY KEY,
    did        TEXT NOT NULL REFERENCES subscribers(did) ON DELETE CASCADE,
    scope      TEXT NOT NULL DEFAULT 'config',   -- 'config' or 'admin'
    expires_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    sid          TEXT PRIMARY KEY,
    did          TEXT NOT NULL REFERENCES subscribers(did) ON DELETE CASCADE,
    scope        TEXT NOT NULL DEFAULT 'config',
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT
);

-- Chat transport bookkeeping, keyed by conversation and holding no DID.
-- Deliberate: this must survive `delete`, or a returning subscriber's old
-- messages get replayed as fresh commands. Spec section 4.
CREATE TABLE IF NOT EXISTS convo_state (
    convo_id             TEXT PRIMARY KEY,
    last_message_id      TEXT,
    last_help_at         TEXT,
    delete_pending_until TEXT
);

CREATE TABLE IF NOT EXISTS league_state (
    league               TEXT PRIMARY KEY,
    last_fetch_at        TEXT,
    last_success_at      TEXT,
    last_event_date      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

-- Append-only, and the only table `delete` does not touch. Without it,
-- churn and retention are unrecoverable the moment a subscriber leaves,
-- which is the population the project exists to measure. Spec section 11.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,           -- UTC
    event_type TEXT NOT NULL,
    subject    TEXT NOT NULL,           -- HMAC-SHA256(did, pepper), never the DID
    league     TEXT,
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_type ON events (event_type);
