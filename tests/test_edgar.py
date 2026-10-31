"""EDGAR parsing. Pure, so saved JSON in, Filings out.

The fixtures are hand-built in EDGAR's shape (a trimmed copy of a real
submissions file's columns), so the tests don't depend on the SEC.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from sportscrawl.edgar import ParseError, accepted_at, is_interesting, parse_submissions, parse_tickers
from sportscrawl.nyse_calendar import NEW_YORK

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_ticker_table():
    table = parse_tickers(load("company_tickers.json"))
    assert table["AAPL"] == 320193
    assert table["BRK-B"] == 1067983


def test_only_the_reports_people_care_about():
    assert is_interesting("10-Q", "")
    assert is_interesting("10-K", "")
    assert is_interesting("8-K", "2.02,9.01")
    assert not is_interesting("8-K", "5.07")  # shareholder vote results
    assert not is_interesting("4", "")  # an insider's share sale
    assert not is_interesting("10-Q/A", "")  # a correction


def test_filings_are_picked_and_linked():
    filings = parse_submissions(load("aapl_submissions.json"), "AAPL", 320193)
    assert [f.form for f in filings] == ["10-K", "8-K", "10-Q", "8-K"]
    assert filings[0].url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000105/aapl-20260926.htm"
    )
    assert filings[0].league == "market/AAPL"
    assert filings[1].label == "earnings release (8-K)"


def test_acceptance_time_is_new_york_time():
    assert accepted_at("2026-10-29T16:31:05.000Z") == datetime(2026, 10, 29, 16, 31, 5,
                                                               tzinfo=NEW_YORK)


def test_garbage_is_an_error_not_a_guess():
    with pytest.raises(ParseError):
        parse_submissions({"filings": {}}, "AAPL", 320193)
    with pytest.raises(ParseError):
        parse_tickers({"0": {"ticker": "AAPL"}})
