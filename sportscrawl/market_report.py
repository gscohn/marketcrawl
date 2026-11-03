"""The operator's daily summary DM.

ELI5: once a day, after the closing business is done (90 minutes after the
bell: the DMs, the price post and the earnings post have all had their
turn), the bot DMs whoever runs it a short report card:

    Mon Nov 2 summary
    Price post: sent
    Earnings post: sent
    Close DMs: 12 sent, 1 "prices missing" notice, 0 not sent

    Since last summary
    New people: 3 (first DM)
    Left: 1 deleted, 0 paused (1 came back)
    Symbols: 7 added, 2 removed

    Now: 40 people following 85 symbols (2 paused)

It goes to ADMIN_DID (from .env). No ADMIN_DID, no summary.

Three parts, like everywhere else in the project:
  - gather(): reads the numbers out of the database. Counts only: like the
    original's admin page, nothing here names anyone.
  - format_summary(): turns the numbers into text. Pure, so it's tested
    without a database.
  - DailySummary: decides whether today's is due and sends it, once.
    "Sent today" is written into the events table, so restarting the
    scheduler doesn't send it twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import repo
from .formatter import _header
from .market_feed import EARNINGS_DID, FEED_DID, FEED_DIDS
from .market_repo import TICKER
from .market_schedule import report_due
from .nyse_calendar import new_york_date, previous_session_day

EVENT = "operator_summary"
# The events the poller writes when someone adds or removes symbols (see
# market_commands.py).
TICKER_ADD = "ticker_add"
TICKER_REMOVE = "ticker_remove"


@dataclass
class Numbers:
    price_post: str  # "sent", "skipped", "not sent", "off"
    earnings_post: str  # "sent", "none", "not sent", "off"
    dms_sent: int = 0
    dm_notices: int = 0  # "No prices available" apologies
    dms_missed: int = 0  # due today, never went
    new_people: int = 0
    deleted: int = 0
    paused: int = 0
    resumed: int = 0
    symbols_added: int = 0
    symbols_removed: int = 0
    people: int = 0  # active, following at least one symbol
    paused_now: int = 0
    symbols: int = 0  # distinct symbols followed
    failing_symbols: int = 0  # symbols whose last download failed
    dry_run: bool = False  # public posts printed, not posted
    errors: int = 0  # send errors the scheduler hit today (since it started)


# -- reading the numbers ---------------------------------------------------

def _feed_status(row, day, nothing: str) -> str:
    """What happened to one feed's post today, from its pretend
    subscriber's bookkeeping."""
    if row is None:
        return "off"
    stamp = day.isoformat()
    if row["last_sent_date"] == stamp:
        return "sent"
    if row["last_evaluated_date"] == stamp:
        return nothing  # looked, had nothing to say
    return "not sent"


def _count(db, event_type, start, end, skip) -> int:
    """How many events of this kind between start and end, leaving out the
    feeds' pretend subscribers (their sign-ups aren't people)."""
    marks = ",".join("?" * len(skip))
    return db.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ? AND ts > ? AND ts <= ?"
        " AND subject NOT IN (%s)" % marks,
        (event_type, repo.utc_string(start), repo.utc_string(end), *skip),
    ).fetchone()[0]


def _sum_detail(db, event_type, start, end) -> int:
    """Add up the "count" in each event's detail (one `add AAPL MSFT` is
    one event with a count of 2)."""
    rows = db.execute(
        "SELECT COALESCE(SUM(json_extract(detail, '$.count')), 0) FROM events"
        " WHERE event_type = ? AND ts > ? AND ts <= ?",
        (event_type, repo.utc_string(start), repo.utc_string(end)),
    ).fetchone()
    return int(rows[0])


def gather(db, day, now: datetime, pepper: str) -> Numbers:
    """The numbers for `day`'s summary. "Since last summary" means since
    the previous trading day's summary was due, so Monday's covers the
    weekend too."""
    start = report_due(previous_session_day(day))
    skip = [repo.subject_for(did, pepper) for did in FEED_DIDS]
    stamp = day.isoformat()

    numbers = Numbers(
        price_post=_feed_status(repo.get_subscriber(db, FEED_DID), day, "skipped"),
        earnings_post=_feed_status(repo.get_subscriber(db, EARNINGS_DID), day, "none"),
    )
    # Today's close DMs: everyone active with a symbol, feeds left out.
    for row in db.execute(
        "SELECT u.last_sent_date, u.last_failure_date FROM subscribers u"
        " WHERE u.state = 'active' AND u.tz IS NOT NULL AND u.did NOT IN (?, ?)"
        " AND EXISTS (SELECT 1 FROM subs s WHERE s.did = u.did AND s.kind = ?)",
        (FEED_DID, EARNINGS_DID, TICKER),
    ):
        if row["last_sent_date"] == stamp:
            numbers.dms_sent += 1
        elif row["last_failure_date"] == stamp:
            numbers.dm_notices += 1
        else:
            numbers.dms_missed += 1
        numbers.people += 1

    numbers.new_people = _count(db, "signup", start, now, skip)
    numbers.deleted = _count(db, "delete", start, now, skip)
    numbers.paused = _count(db, "pause", start, now, skip)
    numbers.resumed = _count(db, "resume", start, now, skip)
    numbers.symbols_added = _sum_detail(db, TICKER_ADD, start, now)
    numbers.symbols_removed = _sum_detail(db, TICKER_REMOVE, start, now)

    numbers.paused_now = db.execute(
        "SELECT COUNT(*) FROM subscribers WHERE state = 'paused'").fetchone()[0]
    numbers.symbols = db.execute(
        "SELECT COUNT(DISTINCT s.league) FROM subs s JOIN subscribers u ON u.did = s.did"
        " WHERE s.kind = ? AND u.state = 'active' AND u.did NOT IN (?, ?)",
        (TICKER, FEED_DID, EARNINGS_DID),
    ).fetchone()[0]
    numbers.failing_symbols = db.execute(
        "SELECT COUNT(*) FROM league_state WHERE consecutive_failures > 0"
        " AND league LIKE 'market/%'").fetchone()[0]
    return numbers


# -- writing it ------------------------------------------------------------

def _plural(n: int, word: str) -> str:
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


_POST_WORDS = {
    "sent": "sent",
    "skipped": "skipped (no index prices)",
    "none": "none today (no big company reported)",
    "no filings": "off (SEC_CONTACT not set, so no filings)",
    "not sent": "NOT SENT",
    "off": "off",
}


def format_summary(numbers: Numbers, day) -> str:
    def post(status):
        words = _POST_WORDS[status]
        if status == "sent" and numbers.dry_run:
            words += " (dry run: printed, not posted)"
        return words

    dms = "Close DMs: %d sent" % numbers.dms_sent
    if numbers.dm_notices:
        dms += ", %s" % _plural(numbers.dm_notices, '"prices missing" notice')
    dms += ", %d not sent" % numbers.dms_missed

    left = "Left: %d deleted, %d paused" % (numbers.deleted, numbers.paused)
    if numbers.resumed:
        left += " (%d came back)" % numbers.resumed

    lines = [
        "%s summary" % _header(day),
        "Price post: %s" % post(numbers.price_post),
        "Earnings post: %s" % post(numbers.earnings_post),
        dms,
        "",
        "Since last summary",
        "New people: %d (first DM)" % numbers.new_people,
        left,
        "Symbols: %d added, %d removed" % (numbers.symbols_added, numbers.symbols_removed),
        "",
        "Now: %d %s following %s (%d paused)" % (
            numbers.people, "person" if numbers.people == 1 else "people",
            _plural(numbers.symbols, "symbol"), numbers.paused_now),
    ]
    problems = []
    if numbers.failing_symbols:
        problems.append("%s failing to download" % _plural(numbers.failing_symbols, "symbol"))
    if numbers.errors:
        problems.append("%s today" % _plural(numbers.errors, "send error"))
    if problems:
        lines += ["", "Problems: " + "; ".join(problems)]
    return "\n".join(lines)


# -- sending it, once a day ------------------------------------------------

class DailySummary:
    """Call after every scheduler tick; it sends at most once per trading
    day, once the summary is due. Same rule as everyone's DMs: more than 90
    minutes late (the computer was off), skip it rather than send stale
    numbers in the middle of the night."""

    LATENESS_WINDOW = timedelta(minutes=90)

    def __init__(self, connection, pepper, admin_did, sender, dry_run=False, filings=True):
        self._db = connection
        self._pepper = pepper
        self._admin = admin_did
        self._send = sender
        self._dry_run = dry_run
        self._filings = filings  # without SEC filings there's no earnings post
        self._errors = {}  # day -> send errors counted from tick reports

    def count_errors(self, now: datetime, errors: int) -> None:
        if errors:
            day = new_york_date(now)
            self._errors[day] = self._errors.get(day, 0) + errors

    def _already_sent(self, due: datetime) -> bool:
        return self._db.execute(
            "SELECT 1 FROM events WHERE event_type = ? AND ts >= ? LIMIT 1",
            (EVENT, repo.utc_string(due)),
        ).fetchone() is not None

    def maybe_send(self, now: datetime) -> bool:
        if not self._admin:
            return False
        day = new_york_date(now)
        due = report_due(day)
        if due is None or now < due or now - due > self.LATENESS_WINDOW:
            return False
        if self._already_sent(due):
            return False
        numbers = gather(self._db, day, now, self._pepper)
        numbers.dry_run = self._dry_run
        numbers.errors = self._errors.get(day, 0)
        if not self._filings and numbers.earnings_post == "none":
            numbers.earnings_post = "no filings"
        try:
            self._send(self._admin, format_summary(numbers, day))
        except Exception as exc:
            # Tried again next tick, like any DM.
            print("summary failed: %s" % exc, flush=True)
            return False
        repo.record_event(self._db, now, EVENT, self._admin, self._pepper)
        return True
