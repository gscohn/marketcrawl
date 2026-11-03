"""The operator's daily summary DM: the numbers, the text, and once a day."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sportscrawl import market_feed, market_repo, repo
from sportscrawl.db import connect, migrate
from sportscrawl.market_report import DailySummary, Numbers, format_summary, gather
from sportscrawl.market_schedule import report_due

PEPPER = "test-pepper"
ADMIN = "did:plc:operator"
MONDAY = date(2026, 11, 2)
DUE = report_due(MONDAY)  # 5:30pm New York
EARLIER = DUE - timedelta(hours=3)


@pytest.fixture
def db(tmp_path):
    connection = connect(str(tmp_path / "r.db"))
    migrate(connection)
    return connection


def person(db, did, symbols, when=EARLIER):
    repo.create_subscriber(db, did, when, PEPPER, tz=market_repo.MARKET_TZ)
    for symbol in symbols:
        market_repo.add_ticker(db, did, symbol)


def test_the_numbers(db):
    market_feed.enable(db, EARLIER - timedelta(days=5), PEPPER)  # not counted as people
    repo.mark_sent(db, market_feed.FEED_DID, MONDAY)
    repo.mark_evaluated(db, market_feed.EARNINGS_DID, MONDAY)
    person(db, "did:plc:a", ["AAPL", "MSFT"])
    person(db, "did:plc:b", ["AAPL"])
    person(db, "did:plc:c", ["NVDA"])
    person(db, "did:plc:gone", ["TSLA"])
    repo.mark_sent(db, "did:plc:a", MONDAY)
    repo.mark_failed(db, "did:plc:b", MONDAY)
    repo.delete_subscriber(db, "did:plc:gone", EARLIER, PEPPER)
    repo.record_event(db, EARLIER, "ticker_add", "did:plc:a", PEPPER, detail={"count": 2})
    repo.record_event(db, EARLIER, "ticker_remove", "did:plc:a", PEPPER, detail={"count": 1})

    n = gather(db, MONDAY, DUE, PEPPER)
    assert (n.price_post, n.earnings_post) == ("sent", "none")
    assert (n.dms_sent, n.dm_notices, n.dms_missed) == (1, 1, 1)
    assert (n.new_people, n.deleted) == (4, 1)
    assert (n.symbols_added, n.symbols_removed) == (2, 1)
    assert (n.people, n.symbols) == (3, 3)


def test_feeds_never_turned_on_say_off(db):
    n = gather(db, MONDAY, DUE, PEPPER)
    assert (n.price_post, n.earnings_post) == ("off", "off")


def test_the_text():
    n = Numbers(price_post="sent", earnings_post="not sent", dms_sent=12, dm_notices=1,
                dms_missed=0, new_people=3, deleted=1, resumed=1, symbols_added=7,
                symbols_removed=2, people=40, paused_now=2, symbols=85, failing_symbols=1)
    assert format_summary(n, MONDAY) == (
        "Mon Nov 2 summary\n"
        "Price post: sent\n"
        "Earnings post: NOT SENT\n"
        'Close DMs: 12 sent, 1 "prices missing" notice, 0 not sent\n'
        "\n"
        "Since last summary\n"
        "New people: 3 (first DM)\n"
        "Left: 1 deleted, 0 paused (1 came back)\n"
        "Symbols: 7 added, 2 removed\n"
        "\n"
        "Now: 40 people following 85 symbols (2 paused)\n"
        "\n"
        "Problems: 1 symbol failing to download"
    )


def test_dry_run_is_spelled_out():
    n = Numbers(price_post="sent", earnings_post="none", dry_run=True)
    assert "Price post: sent (dry run: printed, not posted)" in format_summary(n, MONDAY)


def test_sent_once_after_the_close_business_is_done(db):
    sent = []
    summary = DailySummary(db, PEPPER, ADMIN, lambda did, text: sent.append((did, text)))
    assert not summary.maybe_send(DUE - timedelta(minutes=1))
    assert summary.maybe_send(DUE)
    assert not summary.maybe_send(DUE + timedelta(minutes=1))
    # A restart forgets everything in memory, but not the events table.
    again = DailySummary(db, PEPPER, ADMIN, lambda did, text: sent.append((did, text)))
    assert not again.maybe_send(DUE + timedelta(minutes=2))
    assert [did for did, _ in sent] == [ADMIN]
    assert sent[0][1].startswith("Mon Nov 2 summary")


def test_retried_if_the_dm_fails(db):
    calls = []

    def flaky(did, text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("chat down")

    summary = DailySummary(db, PEPPER, ADMIN, flaky)
    assert not summary.maybe_send(DUE)
    assert summary.maybe_send(DUE + timedelta(minutes=1))


def test_nothing_without_an_operator_or_on_a_holiday_or_far_too_late(db):
    sent = []
    send = lambda did, text: sent.append(did)  # noqa: E731
    assert not DailySummary(db, PEPPER, None, send).maybe_send(DUE)
    saturday = datetime(2026, 11, 7, 23, 0, tzinfo=timezone.utc)
    assert not DailySummary(db, PEPPER, ADMIN, send).maybe_send(saturday)
    assert not DailySummary(db, PEPPER, ADMIN, send).maybe_send(DUE + timedelta(hours=2))
    assert sent == []


def test_send_errors_are_counted_into_the_day(db):
    sent = []
    summary = DailySummary(db, PEPPER, ADMIN, lambda did, text: sent.append(text))
    summary.count_errors(EARLIER, 2)
    summary.maybe_send(DUE)
    assert "2 send errors today" in sent[0]


def test_without_sec_filings_the_earnings_post_reads_off_not_none(db):
    market_feed.enable(db, EARLIER, PEPPER)
    repo.mark_evaluated(db, market_feed.EARNINGS_DID, MONDAY)
    sent = []
    DailySummary(db, PEPPER, ADMIN, lambda did, text: sent.append(text),
                 filings=False).maybe_send(DUE)
    assert "Earnings post: off (SEC_CONTACT not set" in sent[0]
