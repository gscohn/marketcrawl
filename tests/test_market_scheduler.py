"""The original scheduler, driven with the market pieces plugged in."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from run_market import build
from sportscrawl import market_repo, repo
from sportscrawl.http import Fetcher, Response
from sportscrawl.market_source import YahooSource
from sportscrawl.yahoo import chart_url

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
PEPPER = "test-pepper"
DID = "did:plc:investor"
DUE = datetime(2026, 10, 30, 20, 15, tzinfo=timezone.utc)


class Transport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, timeout, headers):
        self.calls.append(url)
        if url == chart_url("AAPL"):
            return Response(200, (FIXTURES / "aapl_5d.json").read_text(encoding="utf-8"))
        return Response(503, "")


@pytest.fixture
def world(tmp_path):
    sent = []
    transport = Transport()
    source = YahooSource(fetcher=Fetcher(transport=transport, sleep=lambda _: None))
    scheduler = build(str(tmp_path / "m.db"), PEPPER,
                      sender=lambda did, text: sent.append((did, text)),
                      source=source)
    scheduler._sleep = lambda _: None
    db = scheduler._db
    repo.create_subscriber(db, DID, DUE, PEPPER)
    market_repo.ensure_market_tz(db, DID)
    return scheduler, db, sent, transport


def test_sends_once_after_the_close(world):
    scheduler, db, sent, _ = world
    market_repo.add_ticker(db, DID, "AAPL")

    assert scheduler.tick(DUE - timedelta(minutes=5)).sent == 0
    assert scheduler.tick(DUE).sent == 1
    assert sent[0][1].startswith("Fri Oct 30 close")
    assert scheduler.tick(DUE + timedelta(minutes=1)).sent == 0


def test_only_fetches_what_people_follow(world):
    scheduler, db, _, transport = world
    market_repo.add_ticker(db, DID, "AAPL")
    scheduler.tick(DUE)
    assert transport.calls == [chart_url("AAPL")]


def test_a_missing_price_is_retried_then_reported(world):
    scheduler, db, sent, _ = world
    market_repo.add_ticker(db, DID, "AAPL")
    market_repo.add_ticker(db, DID, "MSFT")  # the fake Yahoo is down for MSFT

    assert scheduler.tick(DUE).deferred == 1
    report = scheduler.tick(DUE + timedelta(minutes=11))
    assert report.sent == 1
    assert "Some prices are missing." in sent[0][1]


def test_nothing_on_a_weekend(world):
    scheduler, db, sent, transport = world
    market_repo.add_ticker(db, DID, "AAPL")
    saturday = DUE + timedelta(days=1)
    assert scheduler.tick(saturday).sent == 0
    assert transport.calls == []


def test_paused_subscribers_symbols_are_not_fetched(world):
    scheduler, db, _, _ = world
    market_repo.add_ticker(db, DID, "AAPL")
    repo.set_state(db, DID, "paused", DUE, PEPPER)
    assert market_repo.ticker_leagues(db) == []
