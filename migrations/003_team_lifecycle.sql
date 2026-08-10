-- Track whether a team is still in its league.
--
-- The teams table was frozen at first seed: seed_teams.py never deleted, and
-- deploy.sh only ran it when the table was empty. A relegated or renamed club
-- kept its row forever, stayed selectable in the picker, and any subscriber
-- following it got silence indistinguishable from the intentional
-- empty-digest rule.
--
-- Deactivating rather than deleting, because a 200 carrying a truncated
-- roster would otherwise erase real teams, and because an existing
-- subscription should still resolve to a readable name after its club leaves.
ALTER TABLE teams ADD COLUMN last_seen_at TEXT;
ALTER TABLE teams ADD COLUMN active INTEGER NOT NULL DEFAULT 1;
