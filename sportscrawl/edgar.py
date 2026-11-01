"""Reading the SEC's list of a company's filings.

ELI5: every US-listed company has to file reports with the SEC, the
government's stock market referee. The SEC's EDGAR system publishes them all,
free, the moment they arrive. Most are paperwork nobody follows (an executive
sold some shares, a form changed address). A few are what investors actually
wait for:

    10-Q   the quarterly report: three months of results, checked and filed
    10-K   the annual report: the same for a whole year, in more depth
    8-K with "item 2.02"   the earnings release: the press release a company
           puts out on earnings day, usually right at or after the close

This file picks those out of EDGAR's JSON and nothing else. Like yahoo.py it
never touches the network; downloading is edgar_source.py's job, so the tests
can feed in saved JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .nyse_calendar import NEW_YORK
from .yahoo import league_for

ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

# The 8-K "item" number that means "here are our results". An 8-K is a
# catch-all form for news a company must announce; the item numbers inside it
# say what kind of news. 2.02 is "Results of Operations and Financial
# Condition", which in practice is the earnings press release.
EARNINGS_ITEM = "2.02"

# What we call each kind of filing in the message.
LABELS = {
    "10-Q": "quarterly report (10-Q)",
    "10-K": "annual report (10-K)",
    "8-K": "earnings release (8-K)",
}


class ParseError(Exception):
    """The JSON wasn't the EDGAR data we expected."""


@dataclass(frozen=True)
class Filing:
    """One filing worth telling someone about."""

    symbol: str
    form: str
    accepted: datetime  # when the SEC received it
    url: str

    @property
    def league(self) -> str:
        # Same "market/AAPL" key as a Quote, so a filing goes to exactly the
        # people who follow that symbol.
        return league_for(self.symbol)

    @property
    def label(self) -> str:
        return LABELS[self.form]


def parse_tickers(payload: dict) -> dict:
    """SEC's ticker list -> {"AAPL": 320193, ...}.

    The SEC knows companies by a number (the CIK), not by ticker. This file
    is their lookup table. It only covers companies that file with the SEC,
    so indexes (^GSPC), most funds and foreign stocks simply aren't in it.
    """
    try:
        return {row["ticker"].upper(): int(row["cik_str"]) for row in payload.values()}
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ParseError("malformed ticker list: %s" % exc) from exc


def is_interesting(form: str, items: str) -> bool:
    """10-Qs, 10-Ks and earnings 8-Ks. Amendments ("10-Q/A") are left out:
    they're corrections, rarely news."""
    if form in ("10-Q", "10-K"):
        return True
    return form == "8-K" and EARNINGS_ITEM in (items or "").split(",")


def accepted_at(stamp: str) -> datetime:
    """When the SEC accepted a filing.

    EDGAR writes these like "2026-10-30T20:31:05.000Z": UTC, as the "Z"
    says. Checked 2026-10-31 against the SEC's own filing pages, which show
    the same moment in New York time (16:31:05). Turned into New York time
    here, because that's the clock the market keeps.
    """
    naive = datetime.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S")
    return naive.replace(tzinfo=timezone.utc).astimezone(NEW_YORK)


def parse_submissions(payload: dict, symbol: str, cik: int) -> list:
    """One company's recent filings -> the interesting ones, as Filings.

    EDGAR stores these "column-wise": one list of all the form names, one of
    all the dates, and so on, where position 0 in every list is the newest
    filing. zip() lines the columns back up into rows.
    """
    try:
        recent = payload["filings"]["recent"]
        rows = zip(recent["form"], recent["items"], recent["acceptanceDateTime"],
                   recent["accessionNumber"], recent["primaryDocument"])
        filings = []
        for form, items, accepted, accession, document in rows:
            if not is_interesting(form, items):
                continue
            # A filing's address is the company number, the filing's own
            # number without dashes, and the main document's file name.
            url = "%s/%d/%s/%s" % (ARCHIVES, cik, accession.replace("-", ""), document)
            filings.append(Filing(symbol=symbol, form=form,
                                  accepted=accepted_at(accepted), url=url))
        return filings
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError("malformed filings for %s: %s" % (symbol, exc)) from exc
