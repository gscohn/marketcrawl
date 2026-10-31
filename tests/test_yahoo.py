"""Yahoo chart parsing. Saved responses, no network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from sportscrawl.yahoo import ParseError, league_for, parse_chart, quote_for, symbol_of

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
FRIDAY = date(2026, 10, 30)
THURSDAY = date(2026, 10, 29)


def payload(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_reads_name_bars_and_session():
    history = parse_chart(payload("aapl_5d.json"), "AAPL")
    assert history.name == "Apple Inc."
    assert not history.is_index
    assert [bar.day for bar in history.bars][-2:] == [THURSDAY, FRIDAY]
    assert history.session_day == FRIDAY


def test_an_index_is_marked_as_one():
    assert parse_chart(payload("gspc_5d.json"), "^GSPC").is_index


def test_quote_compares_with_the_trading_day_before():
    quote = quote_for(parse_chart(payload("aapl_5d.json"), "AAPL"), FRIDAY)
    assert quote.close == pytest.approx(344.63)
    assert quote.prev_close == pytest.approx(348.91)
    assert quote.pct == pytest.approx(-1.2267, abs=1e-3)
    assert quote.league == "market/AAPL"


def test_an_older_day_is_available_too():
    quote = quote_for(parse_chart(payload("aapl_5d.json"), "AAPL"), THURSDAY)
    assert quote.close == pytest.approx(348.91)


def test_no_quote_for_a_day_with_no_bar():
    history = parse_chart(payload("aapl_5d.json"), "AAPL")
    assert quote_for(history, date(2026, 10, 31)) is None  # Saturday


def test_no_quote_for_the_first_bar_since_there_is_nothing_to_compare():
    history = parse_chart(payload("aapl_5d.json"), "AAPL")
    assert quote_for(history, history.bars[0].day) is None


def test_todays_bar_is_not_a_close_while_still_trading():
    raw = payload("aapl_5d.json")
    meta = raw["chart"]["result"][0]["meta"]
    # Pretend the last trade was an hour before the closing bell.
    meta["regularMarketTime"] = meta["currentTradingPeriod"]["regular"]["end"] - 3600
    history = parse_chart(raw, "AAPL")
    assert quote_for(history, FRIDAY) is None
    # Yesterday's close is finished, though.
    assert quote_for(history, THURSDAY) is not None


def test_a_finished_bar_stays_final_when_the_current_session_is_another_day():
    raw = payload("aapl_5d.json")
    regular = raw["chart"]["result"][0]["meta"]["currentTradingPeriod"]["regular"]
    # On a weekend Yahoo's "current" session is Monday's, which hasn't
    # started. Friday's close must still count.
    regular["end"] += 3 * 86400
    assert quote_for(parse_chart(raw, "AAPL"), FRIDAY) is not None


def test_null_closes_are_skipped_not_zero():
    raw = payload("aapl_5d.json")
    raw["chart"]["result"][0]["indicators"]["quote"][0]["close"][2] = None
    history = parse_chart(raw, "AAPL")
    assert len(history.bars) == 4
    assert all(bar.close > 0 for bar in history.bars)


def test_yahoo_error_payload_is_a_parse_error():
    with pytest.raises(ParseError):
        parse_chart(payload("not_found.json"), "NOTAREALTICKERZZ")


@pytest.mark.parametrize("junk", [{}, {"chart": {}}, {"chart": {"result": []}}, None])
def test_junk_is_a_parse_error(junk):
    with pytest.raises(ParseError):
        parse_chart(junk, "AAPL")


def test_symbol_and_league_round_trip():
    assert symbol_of(league_for("^gspc")) == "^GSPC"


def test_volume_compared_with_the_last_month():
    quote = quote_for(parse_chart(payload("aapl_1mo.json"), "AAPL"), FRIDAY)
    assert quote.volume == 67797500
    assert round(quote.volume_ratio, 2) == 1.75


def test_too_little_history_means_no_volume_verdict():
    quote = quote_for(parse_chart(payload("aapl_5d.json"), "AAPL"), FRIDAY)
    assert quote.volume_ratio is None
