"""EDGAR adapter tests. A fake transport stands in for the SEC."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sportscrawl.edgar_source import SUBMISSIONS_URL, TICKERS_URL, EdgarSource, user_agent
from sportscrawl.http import Fetcher, Response
from sportscrawl.market_schedule import filing_window

FIXTURES = Path(__file__).parent / "fixtures" / "edgar"
NOW = datetime(2026, 10, 30, 20, 15, tzinfo=timezone.utc)
CONTACT = "bot@example.com"


def body(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeTransport:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []
        self.headers = None

    def __call__(self, url, timeout, headers):
        self.calls.append(url)
        self.headers = headers
        return self.replies.get(url, Response(404, "{}"))


def source(replies):
    transport = FakeTransport(replies)
    fetcher = Fetcher(transport=transport, sleep=lambda _: None, user_agent=user_agent(CONTACT))
    return EdgarSource(CONTACT, fetcher=fetcher, sleep=lambda _: None), transport


GOOD = {
    TICKERS_URL: Response(200, body("company_tickers.json")),
    SUBMISSIONS_URL % 320193: Response(200, body("aapl_submissions.json")),
}


def test_only_filings_since_the_last_message():
    # Friday's window: after Thursday 4:15pm, up to Friday 4:15pm.
    edgar, _ = source(GOOD)
    filings = edgar.filings(["AAPL"], *filing_window(NOW.date()), NOW)
    # Thursday's 4:31pm earnings release and Friday's 4:06pm 10-K.
    # Thursday's 9:00am vote results and older reports are not included.
    assert [(f.form, f.accepted.hour) for f in filings] == [("10-K", 16), ("8-K", 16)]


def test_names_a_contact_like_the_sec_asks():
    edgar, transport = source(GOOD)
    edgar.filings(["AAPL"], *filing_window(NOW.date()), NOW)
    assert CONTACT in transport.headers["User-Agent"]


def test_symbols_the_sec_doesnt_know_are_skipped_quietly():
    edgar, transport = source(GOOD)
    assert edgar.filings(["^GSPC", "SHOP.TO"], *filing_window(NOW.date()), NOW) == []
    assert transport.calls == [TICKERS_URL]


def test_an_outage_means_no_filings_not_a_crash():
    edgar, _ = source({TICKERS_URL: Response(200, body("company_tickers.json"))})
    assert edgar.filings(["AAPL", "MSFT"], *filing_window(NOW.date()), NOW) == []
    edgar, _ = source({})
    assert edgar.filings(["AAPL"], *filing_window(NOW.date()), NOW) == []


def test_downloads_are_reused_for_a_few_minutes():
    edgar, transport = source(GOOD)
    window = filing_window(NOW.date())
    edgar.filings(["AAPL"], *window, NOW)
    edgar.filings(["AAPL"], *window, NOW)
    assert transport.calls.count(SUBMISSIONS_URL % 320193) == 1


def test_the_sec_header_is_just_a_name_and_the_contact():
    # The SEC answered 403 when the header also carried a link.
    assert user_agent(CONTACT) == "marketcrawl bot@example.com"
