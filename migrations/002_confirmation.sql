-- Queue a confirmation DM after a subscriber saves their settings.
--
-- The web process must not send it directly: gunicorn runs multiple workers,
-- a chat login per request is slow, and delivery belongs to the scheduler
-- which already owns the sender, the allowlist and per-subscriber error
-- isolation. So the save marks a flag and the scheduler drains it.
--
-- Safe to run twice: SQLite has no ADD COLUMN IF NOT EXISTS, so the runner
-- tolerates the duplicate-column error for this statement only.
ALTER TABLE subscribers ADD COLUMN confirm_pending_at TEXT;
