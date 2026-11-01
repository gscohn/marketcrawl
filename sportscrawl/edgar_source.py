"""The EDGAR adapter: fetches recent company filings from the SEC.

ELI5: for each symbol someone follows, ask the SEC "what has this company
filed lately?", keep only the reports worth mentioning (edgar.py decides
which), and only the ones that arrived since the last message.

Two rules come from the SEC itself (sec.gov/os/accessing-edgar-data):
  - Say who you are, with a way to reach you, in the User-Agent. The SEC
    refuses anonymous requests outright. The contact comes from the
    SEC_CONTACT setting, never from the code, so it isn't published here.
  - No more than 10 requests a second. We pause between companies.

Filings are a nice-to-have. If EDGAR is down, prices still go out; we just
skip the filings and log why.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from .edgar import ParseError, parse_submissions, parse_tickers
from .http import FetchError, Fetcher

log = logging.getLogger("sportscrawl.edgar")

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK%010d.json"

# The ticker -> company number table changes rarely; once a day is plenty.
TICKERS_TTL = timedelta(hours=24)
# A company's filing list: short, so a retry a few minutes later sees an
# earnings release that has only just landed.
SUBMISSIONS_TTL = timedelta(minutes=5)
# A tenth of a second between requests keeps us under the SEC's limit.
PAUSE_SECONDS = 0.15


def user_agent(contact: str) -> str:
    """Just a name and the contact email, the shape the SEC's own example
    uses. Checked 2026-10-31: adding a "(+https://...)" link, as the Yahoo
    header does, gets a 403 from the SEC."""
    return "marketcrawl %s" % contact


@dataclass
class _Cached:
    fetched_at: datetime
    value: object


class EdgarSource:
    def __init__(self, contact: str, fetcher=None, sleep=time.sleep):
        self._fetcher = fetcher or Fetcher(user_agent=user_agent(contact))
        self._sleep = sleep
        self._tickers = None
        self._filings = {}

    def _cik_table(self, now: datetime) -> dict:
        if self._tickers is None or now - self._tickers.fetched_at >= TICKERS_TTL:
            self._tickers = _Cached(now, parse_tickers(self._fetcher.get_json(TICKERS_URL)))
        return self._tickers.value

    def _recent(self, symbol: str, cik: int, now: datetime) -> list:
        cached = self._filings.get(symbol)
        if cached is not None and now - cached.fetched_at < SUBMISSIONS_TTL:
            return cached.value
        self._sleep(PAUSE_SECONDS)
        filings = parse_submissions(self._fetcher.get_json(SUBMISSIONS_URL % cik), symbol, cik)
        self._filings[symbol] = _Cached(now, filings)
        return filings

    def filings(self, symbols, start: datetime, end: datetime, now: datetime) -> list:
        """Interesting filings accepted after `start`, up to and including
        `end`, for these symbols. Never raises: a problem means fewer
        filings, not a missing message."""
        try:
            ciks = self._cik_table(now)
        except (FetchError, ParseError) as exc:
            log.warning("edgar ticker list unavailable: %s", exc)
            return []

        found = []
        for symbol in symbols:
            # Yahoo writes share classes with a dash (BRK-B), and so does the
            # SEC's table. Indexes and foreign stocks aren't in it: skip.
            cik = ciks.get(symbol.upper())
            if cik is None:
                continue
            try:
                recent = self._recent(symbol, cik, now)
            except (FetchError, ParseError) as exc:
                log.warning("edgar filings unavailable for %s: %s", symbol, exc)
                continue
            found.extend(f for f in recent if start < f.accepted <= end)
        return found
