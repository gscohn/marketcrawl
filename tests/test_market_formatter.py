"""Market message formatting. Pure function, so plain inputs and outputs."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sportscrawl.formatter import MAX_MESSAGE
from sportscrawl.market_formatter import FAILURE_NOTICE, MISSING_DATA_LINE, format_market_digest
from sportscrawl.models import Sub
from sportscrawl.yahoo import Quote

FRIDAY = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, 20, 15, tzinfo=timezone.utc)
TZ = "America/New_York"


def quote(symbol, close, prev, day=FRIDAY, index=False, name=None):
    return Quote(symbol=symbol, name=name or symbol, is_index=index,
                 close=close, prev_close=prev, close_date=day)


def follows(*symbols):
    return [Sub(kind="ticker", league="market/%s" % s) for s in symbols]


def test_the_whole_message():
    quotes = [
        quote("AAPL", 344.63, 348.91),
        quote("^GSPC", 7580.07, 7720.39, index=True, name="S&P 500"),
        quote("KO", 70.00, 70.00),
    ]
    digest = format_market_digest(quotes, follows("AAPL", "^GSPC", "KO"), TZ, NOW)
    assert digest.kind == "digest"
    assert digest.text == (
        "Fri Oct 30 close\n"
        "\n"
        "\N{CHART WITH DOWNWARDS TREND} S&P 500 7,580.07 -1.82%\n"
        "\N{CHART WITH DOWNWARDS TREND} AAPL 344.63 -1.23%\n"
        "\N{HEAVY MINUS SIGN} KO 70.00 +0.00%"
    )


def test_only_symbols_this_person_follows():
    quotes = [quote("AAPL", 2, 1), quote("MSFT", 2, 1)]
    text = format_market_digest(quotes, follows("MSFT"), TZ, NOW).text
    assert "MSFT" in text and "AAPL" not in text


def test_an_old_close_is_not_reported_as_todays():
    quotes = [quote("AAPL", 2, 1, day=date(2026, 10, 29))]
    assert format_market_digest(quotes, follows("AAPL"), TZ, NOW).kind == "silent"


def test_following_nothing_sends_nothing():
    assert format_market_digest([quote("AAPL", 2, 1)], [], TZ, NOW).kind == "silent"


def test_sports_subscriptions_are_ignored():
    subs = [Sub(kind="team", league="baseball/mlb", team_id="19")]
    assert format_market_digest([], subs, TZ, NOW).kind == "silent"


def test_some_missing_says_so():
    digest = format_market_digest([quote("AAPL", 2, 1)], follows("AAPL", "MSFT"),
                                  TZ, NOW, failed_leagues={"market/MSFT"})
    assert digest.kind == "partial"
    assert digest.text.endswith(MISSING_DATA_LINE)


def test_all_missing_is_an_apology():
    digest = format_market_digest([], follows("AAPL"), TZ, NOW,
                                  failed_leagues={"market/AAPL"})
    assert digest.kind == "failure"
    assert digest.text == FAILURE_NOTICE


def test_never_over_the_message_limit():
    symbols = ["S%03d" % i for i in range(80)]
    quotes = [quote(s, 1234.56, 1200.00) for s in symbols]
    text = format_market_digest(quotes, follows(*symbols), TZ, NOW).text
    assert len(text) <= MAX_MESSAGE
    assert "more" in text.splitlines()[-1]
