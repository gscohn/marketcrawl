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


# -- a newcomer's first report ---------------------------------------------

from datetime import date  # noqa: E402

from sportscrawl.market_commands import FIRST_REPORT_INTRO  # noqa: E402
from sportscrawl.yahoo import Quote  # noqa: E402

THURSDAY = date(2026, 10, 29)
FRIDAY = date(2026, 10, 30)
FRIDAY_EVENING = datetime(2026, 10, 30, 23, 0, tzinfo=timezone.utc)  # 7pm New York


def latest_on(day):
    def latest(symbols, now):
        return [Quote(symbol=s, name=KNOWN.get(s, s), is_index=False, close=101.0,
                      prev_close=100.0, close_date=day) for s in symbols]
    return latest


def newcomer_bot(db, day):
    handler = MarketCommandHandler(db, PEPPER, lookup=fake_lookup, latest=latest_on(day))

    def say(text, now):
        return handler.handle(Message(message_id="m", convo_id=CONVO, sender_did=DID,
                                      text=text, sent_at=""), now)
    return say


def last_evaluated(db):
    return db.execute("SELECT last_evaluated_date FROM subscribers WHERE did = ?",
                      (DID,)).fetchone()[0]


def test_first_add_comes_with_the_latest_close(db):
    # 10am Friday: markets open, so the latest finished close is Thursday's.
    reply = newcomer_bot(db, THURSDAY)("add AAPL", NOW)
    assert FIRST_REPORT_INTRO in reply
    assert "Thu Oct 29 close" in reply and "AAPL 101.00 +1.00%" in reply
    # Thursday's close doesn't count as Friday's message: Friday's still comes.
    assert last_evaluated(db) is None


def test_after_the_close_the_first_report_is_todays_message(db):
    reply = newcomer_bot(db, FRIDAY)("add AAPL", FRIDAY_EVENING)
    assert "Fri Oct 30 close" in reply
    # Recorded as sent, so the scheduler doesn't send Friday's close again.
    assert last_evaluated(db) == "2026-10-30"


def test_only_the_first_add_gets_a_report(db):
    bot = newcomer_bot(db, THURSDAY)
    bot("add AAPL", NOW)
    assert FIRST_REPORT_INTRO not in bot("add MSFT", NOW)


def test_no_report_when_prices_are_unavailable(db):
    handler = MarketCommandHandler(db, PEPPER, lookup=fake_lookup, latest=lambda s, n: [])
    reply = handler.handle(Message(message_id="m", convo_id=CONVO, sender_did=DID,
                                   text="add AAPL", sent_at=""), NOW)
    assert FIRST_REPORT_INTRO not in reply
    assert reply.endswith("You'll get closing prices after each market close.")


def test_a_report_that_would_not_fit_is_skipped(db):
    symbols = " ".join("S%03d" % i for i in range(20))
    reply = newcomer_bot(db, THURSDAY)("add " + symbols, NOW)
    assert len(reply) <= 1000
