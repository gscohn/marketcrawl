"""DM command tests. A fake lookup stands in for Yahoo."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sportscrawl import market_repo, repo
from sportscrawl.chat import Message
from sportscrawl.db import connect, migrate
from sportscrawl.http import FetchError
from sportscrawl.market_commands import HELP, MAX_TICKERS, NUDGE, MarketCommandHandler, parse

PEPPER = "test-pepper"
DID = "did:plc:investor"
CONVO = "convo-1"
NOW = datetime(2026, 10, 30, 14, 0, tzinfo=timezone.utc)

KNOWN = {"AAPL": "Apple Inc.", "^GSPC": "S&P 500", "BRK-B": "Berkshire",
         "SHOP.TO": "Shopify", "MSFT": "Microsoft"}


def fake_lookup(symbol, now):
    if symbol == "DOWN":
        raise FetchError("http 503")
    if symbol in KNOWN:
        return SimpleNamespace(name=KNOWN[symbol])
    if symbol.startswith("S") and symbol[1:].isdigit():  # S001, S002... for the cap
        return SimpleNamespace(name=symbol)
    return None


@pytest.fixture
def db(tmp_path):
    connection = connect(str(tmp_path / "cmd.db"))
    migrate(connection)
    return connection


@pytest.fixture
def bot(db):
    handler = MarketCommandHandler(db, PEPPER, lookup=fake_lookup)

    def say(text, now=NOW):
        message = Message(message_id="m", convo_id=CONVO, sender_did=DID,
                          text=text, sent_at="")
        return handler.handle(message, now)

    return say


# -- parsing ----------------------------------------------------------------


@pytest.mark.parametrize("text, verb, symbols", [
    ("add AAPL", "add", ["AAPL"]),
    ("ADD aapl, msft", "add", ["AAPL", "MSFT"]),
    ("Add $AAPL!", "add", ["AAPL"]),
    ("add sp500 dow nasdaq", "add", ["^GSPC", "^DJI", "^IXIC"]),
    ("add ^GSPC BRK.B", "add", ["^GSPC", "BRK.B"]),
    ("rm aapl", "rm", ["AAPL"]),
    ("", "", []),
])
def test_parse(text, verb, symbols):
    assert parse(text) == (verb, symbols)


# -- add / remove / list -------------------------------------------------------


def test_add_follows_and_echoes_the_name(bot, db):
    reply = bot("add AAPL")
    assert "Added AAPL (Apple Inc.)" in reply
    assert market_repo.tickers_for(db, DID) == ["AAPL"]


def test_first_add_switches_delivery_on(db, bot):
    bot("hello")
    assert repo.get_subscriber(db, DID)["tz"] is None
    bot("add AAPL")
    assert repo.get_subscriber(db, DID)["tz"] == "America/New_York"


def test_add_several_at_once_with_an_alias(bot, db):
    bot("add aapl sp500")
    assert market_repo.tickers_for(db, DID) == ["AAPL", "^GSPC"]


def test_dot_share_class_becomes_yahoos_dash(bot, db):
    assert "Added BRK-B" in bot("add BRK.B")


def test_a_real_dotted_symbol_is_kept(bot, db):
    assert "Added SHOP.TO" in bot("add shop.to")


def test_unknown_symbol(bot, db):
    assert "Couldn't find APPL" in bot("add APPL")
    assert market_repo.tickers_for(db, DID) == []
    # Not turned on by a failed add.
    assert repo.get_subscriber(db, DID)["tz"] is None


def test_yahoo_down_is_not_blamed_on_the_user(bot):
    assert "right now" in bot("add DOWN")


def test_nonsense_is_rejected_without_a_lookup(bot):
    assert "doesn't look like a symbol" in bot("add hello/world")


def test_duplicate_add(bot):
    bot("add AAPL")
    assert "Already following AAPL" in bot("add aapl")


def test_the_cap(bot, db):
    bot("add " + " ".join("S%03d" % i for i in range(MAX_TICKERS)))
    assert len(market_repo.tickers_for(db, DID)) == MAX_TICKERS
    assert "limit" in bot("add AAPL")


def test_add_with_nothing(bot):
    assert "Which symbol" in bot("add")


def test_remove(bot, db):
    bot("add AAPL MSFT")
    assert "Removed AAPL" in bot("remove aapl")
    assert market_repo.tickers_for(db, DID) == ["MSFT"]


def test_remove_something_not_followed(bot):
    assert "weren't following" in bot("rm TSLA")


def test_removing_the_last_one_says_messages_stop(bot):
    bot("add AAPL")
    assert "not following anything" in bot("drop AAPL")


def test_list(bot):
    assert "not following anything" in bot("list")
    bot("add MSFT AAPL")
    assert bot("list") == "You follow (2 of 20):\nAAPL MSFT"


# -- help -----------------------------------------------------------------------


def test_help_lists_the_commands(bot):
    reply = bot("help")
    for word in ("add", "remove", "list", "stop", "delete", "help"):
        assert word in reply


def test_unknown_message_gets_help_then_a_nudge(bot):
    assert bot("hi there") == HELP
    assert bot("thanks", NOW + timedelta(minutes=5)) == NUDGE
    assert bot("hey", NOW + timedelta(hours=2)) == HELP


def test_web_page_commands_just_get_help(bot):
    assert bot("config") == HELP


# -- the original commands still work ------------------------------------------


def test_stop_and_start(bot, db):
    bot("add AAPL")
    bot("stop")
    assert repo.get_subscriber(db, DID)["state"] == "paused"
    assert "next market close" in bot("start")
    assert repo.get_subscriber(db, DID)["state"] == "active"


def test_delete_needs_confirming_and_removes_everything(bot, db):
    bot("add AAPL")
    assert "symbols you follow" in bot("delete")
    bot("delete confirm")
    assert repo.get_subscriber(db, DID) is None
    assert market_repo.tickers_for(db, DID) == []


def test_another_command_cancels_a_pending_delete(bot, db):
    bot("add AAPL")
    bot("delete")
    bot("list")
    # "confirm" alone now starts a fresh delete rather than finishing one.
    assert "delete confirm" in bot("confirm")
    assert repo.get_subscriber(db, DID) is not None


# -- privacy ---------------------------------------------------------------------


class Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def test_the_log_names_the_command_never_the_text(bot):
    # A handler on the logger itself rather than caplog: other tests call
    # logs.configure(), which stops records reaching pytest's capture.
    root = logging.getLogger("sportscrawl")
    collect, level = Collect(), root.level
    root.addHandler(collect)
    root.setLevel(logging.INFO)
    try:
        bot("add AAPL")
        bot("my secret message")
    finally:
        root.removeHandler(collect)
        root.setLevel(level)
    logged = " ".join(collect.lines)
    assert "matched=add" in logged
    assert "AAPL" not in logged
    assert "secret" not in logged


def test_about_credits_sportscrawl(bot):
    from sportscrawl.market_commands import ABOUT

    assert bot("about") == ABOUT
    assert "SportsCrawl" in ABOUT and "cruftbox" in ABOUT
    assert "SportsCrawl" in HELP


# -- test: the message for the latest close, on demand ------------------------

from datetime import date  # noqa: E402

from sportscrawl.market_commands import (  # noqa: E402
    ADDED_OUTRO, TEST_COOLDOWN, TEST_HOURLY_LIMIT)
from sportscrawl.yahoo import Quote  # noqa: E402

THURSDAY = date(2026, 10, 29)


def latest_on(day, calls=None):
    def latest(symbols, now):
        if calls is not None:
            calls.append(list(symbols))
        return [Quote(symbol=s, name=KNOWN.get(s, s), is_index=False, close=101.0,
                      prev_close=100.0, close_date=day) for s in symbols]
    return latest


def bot_with(db, latest):
    handler = MarketCommandHandler(db, PEPPER, lookup=fake_lookup, latest=latest)

    def say(text, now=NOW, did=DID):
        return handler.handle(Message(message_id="m", convo_id="c-" + did, sender_did=did,
                                      text=text, sent_at=""), now)
    return say


def last_evaluated(db):
    return db.execute("SELECT last_evaluated_date FROM subscribers WHERE did = ?",
                      (DID,)).fetchone()[0]


def test_add_just_confirms_and_points_at_test(db):
    reply = bot_with(db, latest_on(THURSDAY))("add AAPL")
    assert reply == "Added AAPL (Apple Inc.).\n\n" + ADDED_OUTRO


def test_test_sends_the_latest_close(db):
    bot = bot_with(db, latest_on(THURSDAY))
    bot("add AAPL")
    reply = bot("test")
    assert "Thu Oct 29 close" in reply and "AAPL 101.00 +1.00%" in reply
    # A preview, not the day's message: the regular one still comes.
    assert last_evaluated(db) is None


def test_test_with_nothing_followed(db):
    assert "not following anything" in bot_with(db, latest_on(THURSDAY))("test")


def test_test_is_once_per_person_per_cooldown(db):
    calls = []
    bot = bot_with(db, latest_on(THURSDAY, calls))
    bot("add AAPL")
    bot("test")
    assert bot("test", NOW + timedelta(minutes=3)) == "You can test again in 7 min."
    assert len(calls) == 1  # the refused one never touched Yahoo
    assert "close" in bot("test", NOW + TEST_COOLDOWN)


def test_test_has_a_shared_hourly_limit(db):
    calls = []
    bot = bot_with(db, latest_on(THURSDAY, calls))
    for i in range(TEST_HOURLY_LIMIT + 1):
        bot("add AAPL", did="did:plc:p%d" % i)
    for i in range(TEST_HOURLY_LIMIT):
        assert "close" in bot("test", did="did:plc:p%d" % i)
    late = "did:plc:p%d" % TEST_HOURLY_LIMIT
    assert bot("test", did=late).startswith("Lots of tests")
    assert len(calls) == TEST_HOURLY_LIMIT
    # An hour later the window has moved on.
    assert "close" in bot("test", NOW + timedelta(hours=1), did=late)


def test_test_when_prices_are_unavailable(db):
    bot = bot_with(db, lambda s, n: [])
    bot("add AAPL")
    assert bot("test") == "Couldn't get prices right now. Try again later."


def test_test_when_yahoo_is_down(db):
    def down(symbols, now):
        raise FetchError("http 503")
    bot = bot_with(db, down)
    bot("add AAPL")
    assert bot("test") == "Couldn't get prices right now. Try again later."


def test_help_lists_test_and_privacy_and_fits(bot):
    assert "test - " in HELP and "privacy - " in HELP
    assert len(HELP) <= 1000


# -- one huge message can't jam the bot -----------------------------------------


def test_a_flood_of_made_up_symbols_stops_early(db):
    calls = []

    def counting_lookup(symbol, now):
        calls.append(symbol)
        return fake_lookup(symbol, now)
    handler = MarketCommandHandler(db, PEPPER, lookup=counting_lookup)
    text = "add " + " ".join("ZZQ%d" % i for i in range(300))
    reply = handler.handle(Message(message_id="m", convo_id=CONVO, sender_did=DID,
                                   text=text, sent_at=""), NOW)
    assert len(calls) <= 5
    assert "Stopped after 5" in reply and "Only the first 20" in reply
    assert len(reply) <= 1000


def test_a_huge_remove_still_fits_in_one_reply(bot):
    reply = bot("remove " + " ".join("x" * 40 for _ in range(300)))
    assert len(reply) <= 1000
    assert "X" * 13 not in reply  # long words are shortened when echoed


def test_dots_alone_are_not_a_symbol(bot):
    assert "doesn't look like a symbol" in bot("add ..")


# -- quote: the price right now, during market hours ---------------------------

from sportscrawl.market_commands import (  # noqa: E402
    MAX_QUOTE_SYMBOLS, QUOTE_COOLDOWN, QUOTE_HOURLY_SYMBOLS, Ration)

MONDAY_2PM = datetime(2026, 11, 2, 19, 31, tzinfo=timezone.utc)  # 2:31pm New York
SATURDAY = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)


def live_prices(calls=None):
    def live(symbols, now):
        if calls is not None:
            calls.append(list(symbols))
        return [(Quote(symbol=s, name=KNOWN.get(s, s), is_index=s.startswith("^"),
                       close=102.0, prev_close=100.0, close_date=now.date()), now)
                for s in symbols if s in KNOWN]
    return live


def quoter(db, live):
    handler = MarketCommandHandler(db, PEPPER, lookup=fake_lookup, live=live)

    def say(text, now=MONDAY_2PM, did=DID):
        return handler.handle(Message(message_id="m", convo_id="c-" + did, sender_did=did,
                                      text=text, sent_at=""), now)
    return say


def test_quote_during_market_hours(db):
    reply = quoter(db, live_prices())("quote AAPL sp500")
    assert reply.startswith("Prices at 2:31pm ET\n\n")
    # Index first, then stocks, same lines as the after-close message.
    assert reply.index("S&P 500") < reply.index("AAPL 102.00 +2.00%")


def test_quote_alone_means_my_list(db):
    calls = []
    bot = quoter(db, live_prices(calls))
    bot("add AAPL MSFT")
    bot("quote")
    assert calls == [["AAPL", "MSFT"]]


def test_quote_when_the_market_is_closed(db):
    calls = []
    reply = quoter(db, live_prices(calls))("quote AAPL", SATURDAY)
    assert reply.startswith("The market's closed.")
    assert "Next open: Mon Nov 2, 9:30am ET." in reply
    assert calls == []


def test_quote_names_what_it_could_not_find(db):
    reply = quoter(db, live_prices())("quote AAPL NOPE")
    assert reply.endswith("Couldn't find: NOPE")


def test_quote_reads_at_most_five_symbols(db):
    calls = []
    quoter(db, live_prices(calls))("quote AAPL MSFT BRK-B SHOP.TO ^GSPC S001 S002")
    assert len(calls[0]) == MAX_QUOTE_SYMBOLS


def test_quote_is_once_a_minute_per_person(db):
    calls = []
    bot = quoter(db, live_prices(calls))
    bot("quote AAPL")
    assert bot("quote AAPL", MONDAY_2PM + timedelta(seconds=20)) == \
        "You can quote again in 40 sec."
    assert len(calls) == 1
    assert "Prices at" in bot("quote AAPL", MONDAY_2PM + QUOTE_COOLDOWN)


def test_quote_shared_limit_counts_symbols():
    ration = Ration(QUOTE_COOLDOWN, QUOTE_HOURLY_SYMBOLS)
    for i in range(QUOTE_HOURLY_SYMBOLS // 5):
        assert ration.take("p%d" % i, MONDAY_2PM, cost=5) is None
    assert ration.take("late", MONDAY_2PM, cost=1) == ("busy", None)
    assert ration.take("late", MONDAY_2PM + timedelta(hours=1), cost=1) is None


def test_quote_when_yahoo_is_down(db):
    def down(symbols, now):
        raise FetchError("http 503")
    assert quoter(db, down)("quote AAPL") == "Couldn't get prices right now. Try again later."


def test_quote_refuses_junk(db):
    assert "doesn't look like a symbol" in quoter(db, live_prices())("quote ..")
