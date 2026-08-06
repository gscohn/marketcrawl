"""Scheduler and delivery.

Runs in its own process. It must NOT run inside the Flask app: gunicorn runs
multiple workers and each would fire the same job, sending duplicate digests.
Spec section 3.

Build step 3 sends to stdout. The chat transport arrives in step 4 and slots
in as a different sender.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import repo
from .confirm import confirmation_text
from .formatter import format_digest

# How late a digest may be delivered. Spec section 7.
#
# Not exact-minute equality: if the process was blocked or the box was down at
# 07:00, a strict check drops the day entirely. Not unbounded either: someone
# signing up at 15:00 with a 07:00 send time would get an instant digest, and
# an outage until mid afternoon would deliver a breakfast digest at teatime.
LATENESS_WINDOW = timedelta(minutes=90)

# How long to keep retrying a failing league before apologising for it. A
# three minute ESPN blip landing on 07:00 should not cost anyone their digest,
# and a notice ninety minutes late is worse than no notice.
FAILURE_GRACE = timedelta(minutes=10)

# Gap between messages in a batch. Users pick round numbers, so 07:00 and
# 08:00 fire simultaneously across four timezones; a queue drain taking ninety
# seconds is invisible to recipients and stays clear of chat rate limits.
SEND_GAP_SECONDS = 0.5

# Consecutive failing ticks before the operator is told, and how long before
# saying it again. Three minutes of failure is a real fault, not a blip; an
# hour of quiet after that stops a broken morning becoming sixty DMs.
ALERT_AFTER_TICKS = 3
ALERT_COOLDOWN = timedelta(hours=1)

NOT_DUE = "not_due"
DUE = "due"
DEFER = "defer"
EXPIRED = "expired"


@dataclass
class Candidate:
    did: str
    tz: str
    local_now: datetime
    local_date: object
    lateness: timedelta


@dataclass
class TickReport:
    sent: int = 0
    failures: int = 0
    silent: int = 0
    expired: int = 0
    deferred: int = 0
    errors: int = 0
    confirmations: int = 0

    def __str__(self) -> str:
        return (
            "sent=%d failures=%d silent=%d expired=%d deferred=%d errors=%d"
            " confirmations=%d"
            % (self.sent, self.failures, self.silent, self.expired,
               self.deferred, self.errors, self.confirmations)
        )


def stdout_sender(did: str, text: str) -> None:
    print("--- to %s" % did)
    print(text)
    print()


def classify(row, now: datetime):
    """Where a subscriber sits relative to their send time."""
    zone = ZoneInfo(row["tz"])
    local_now = now.astimezone(zone)
    local_date = local_now.date()

    if row["last_evaluated_date"] == local_date.isoformat():
        return NOT_DUE, None

    hour, minute = (int(part) for part in row["send_time"].split(":"))
    due_at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    lateness = local_now - due_at

    candidate = Candidate(
        did=row["did"],
        tz=row["tz"],
        local_now=local_now,
        local_date=local_date,
        lateness=lateness,
    )

    if lateness < timedelta(0):
        return NOT_DUE, candidate
    if lateness > LATENESS_WINDOW:
        return EXPIRED, candidate
    return DUE, candidate


class Scheduler:
    def __init__(
        self,
        connection,
        source,
        leagues,
        pepper,
        sender=stdout_sender,
        sleep=time.sleep,
        gap=SEND_GAP_SECONDS,
        alert_did=None,
    ):
        self._db = connection
        self._source = source
        self._leagues = list(leagues)
        self._pepper = pepper
        self._send = sender
        self._sleep = sleep
        self._gap = gap
        self._alert_did = alert_did
        self._failing_ticks = 0
        self._last_error = ""
        self._alerted_at = None

    def tick(self, now: datetime) -> TickReport:
        report = TickReport()
        try:
            self._run(now, report)
        finally:
            # Every exit path, not just the one where somebody was due. An
            # earlier version returned early on a quiet tick, so a failing
            # confirmation loop never alerted and the consecutive counter was
            # never reset: three failures hours apart read as three in a row.
            self._maybe_alert(report, now)
        return report

    def _run(self, now: datetime, report: TickReport) -> None:
        self._send_confirmations(now, report)
        rows = repo.deliverable_subscribers(self._db)

        due = []
        for row in rows:
            verdict, candidate = classify(row, now)
            if verdict == EXPIRED:
                # Past the window. A missing digest is a smaller failure than
                # one arriving when it is no longer true.
                repo.mark_evaluated(self._db, candidate.did, candidate.local_date)
                report.expired += 1
            elif verdict == DUE:
                due.append(candidate)

        if not due:
            return

        window = self._source.fetch_window(self._leagues, now)
        for health in self._source.health.values():
            repo.save_league_health(self._db, health)

        for candidate in due:
            try:
                self._deliver(candidate, window, now, report)
            except Exception as exc:
                # One subscriber's failure must not cost everyone else their
                # digest. Without this, a single bad send aborts the batch and
                # the supervisor restarts the process into the same failure on
                # the next tick.
                #
                # The row is deliberately left unevaluated, so the subscriber
                # is retried on the next tick while they are still inside the
                # lateness window, and dropped quietly once past it.
                report.errors += 1
                self._last_error = "%s" % exc
                print("send failed for %s: %s" % (candidate.did, exc), flush=True)
            self._sleep(self._gap)

    def _maybe_alert(self, report, now) -> None:
        """Tell the operator when deliveries keep failing.

        Two silent outages happened before this existed. Both were found by a
        subscriber noticing a missing digest, because a loop that ticks
        healthily while failing every send looks identical to a quiet morning.
        """
        if not report.errors:
            self._failing_ticks = 0
            return

        self._failing_ticks += 1
        if self._failing_ticks < ALERT_AFTER_TICKS or not self._alert_did:
            return
        if self._alerted_at and now - self._alerted_at < ALERT_COOLDOWN:
            return

        text = "\n".join([
            "SportsCrawl alert",
            "",
            "Digest delivery has failed on %d consecutive ticks." % self._failing_ticks,
            "",
            self._last_error or "no detail captured",
        ])
        try:
            self._send(self._alert_did, text)
            self._alerted_at = now
            print("alert sent to operator", flush=True)
        except Exception as exc:
            # The alert uses the same transport that is failing, so this is
            # expected during a total outage. Never let it mask the original.
            print("alert could not be sent: %s" % exc, flush=True)

    def _send_confirmations(self, now, report) -> None:
        """Tell someone their settings took, and when to expect the first
        digest. Drained here rather than sent by the web process."""
        for row in repo.pending_confirmations(self._db):
            try:
                subs = repo.subs_for(self._db, row["did"])
                teams = sum(1 for sub in subs if sub.kind == "team")
                competitions = sum(1 for sub in subs if sub.kind == "competition")
                local_today = now.astimezone(ZoneInfo(row["tz"])).date().isoformat()

                text = confirmation_text(
                    row["send_time"],
                    row["tz"],
                    teams,
                    competitions,
                    now,
                    already_sent_today=row["last_sent_date"] == local_today,
                )
                self._send(row["did"], text)
                # Cleared only after the send succeeds, so a transport failure
                # retries on the next tick instead of losing the message.
                repo.clear_confirmation(self._db, row["did"])
                report.confirmations += 1
            except Exception as exc:
                report.errors += 1
                self._last_error = "%s" % exc
                print("confirmation failed for %s: %s" % (row["did"], exc), flush=True)

    def _deliver(self, candidate, window, now, report) -> None:
        # Re-check immediately before sending. A delete landing mid-cycle must
        # not result in messaging someone after telling them they were removed.
        row = repo.get_subscriber(self._db, candidate.did)
        if row is None or row["state"] != "active":
            return
        if row["last_evaluated_date"] == candidate.local_date.isoformat():
            return

        subs = repo.subs_for(self._db, candidate.did)
        subscribed = {sub.league for sub in subs}
        failing = subscribed & window.failed_leagues

        if failing and candidate.lateness < FAILURE_GRACE:
            # Retry before apologising. Leaving the row unevaluated is what
            # makes the next tick pick it up again.
            report.deferred += 1
            return

        digest = format_digest(
            window.events,
            subs,
            row["tz"],
            candidate.local_now,
            window.failed_leagues,
        )

        stamp = candidate.local_date

        if digest.kind == "silent":
            repo.mark_evaluated(self._db, candidate.did, stamp)
            report.silent += 1
            return

        if digest.kind == "failure":
            # Once per subscriber per day, maximum. A single outage must not
            # become 200 apologies.
            if row["last_failure_date"] == stamp.isoformat():
                repo.mark_evaluated(self._db, candidate.did, stamp)
                report.silent += 1
                return
            self._send(candidate.did, digest.text)
            repo.mark_failed(self._db, candidate.did, stamp)
            repo.record_event(
                self._db, now, "failure_notice", candidate.did, self._pepper
            )
            report.failures += 1
            return

        self._send(candidate.did, digest.text)

        # Per message commit, not per batch. A crash mid-batch must resume,
        # not re-send, which is also what makes a deploy at 07:05 safe.
        if digest.kind == "partial":
            repo.mark_partial(self._db, candidate.did, stamp)
        else:
            repo.mark_sent(self._db, candidate.did, stamp)

        repo.record_event(
            self._db,
            now,
            "digest_sent",
            candidate.did,
            self._pepper,
            detail={"kind": digest.kind},
        )
        report.sent += 1
