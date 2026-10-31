"""Database helpers for ticker subscriptions.

ELI5: the original database already has a `subs` table with a row for each
thing someone follows: (who, kind, league, team). We reuse it as-is. Following
AAPL is just a row with kind="ticker" and league="market/AAPL". No new
tables, no migration.

Kept in its own file rather than added to repo.py so the original file stays
exactly as its author wrote it, which keeps pulling their updates painless.
"""

from __future__ import annotations

from .yahoo import league_for, symbol_of

TICKER = "ticker"

# Every subscriber gets this timezone. The scheduler skips anyone without one
# (it means "never finished setup"), and market messages go out on New York's
# clock, not the reader's.
MARKET_TZ = "America/New_York"


def ticker_leagues(connection) -> list:
    """Every symbol anyone follows, as leagues. The scheduler fetches these."""
    rows = connection.execute(
        "SELECT DISTINCT s.league FROM subs s JOIN subscribers u ON u.did = s.did"
        " WHERE s.kind = ? AND u.state = 'active' ORDER BY s.league",
        (TICKER,),
    ).fetchall()
    return [row["league"] for row in rows]


def tickers_for(connection, did) -> list:
    rows = connection.execute(
        "SELECT league FROM subs WHERE did = ? AND kind = ? ORDER BY league",
        (did, TICKER),
    ).fetchall()
    return [symbol_of(row["league"]) for row in rows]


def add_ticker(connection, did, symbol) -> bool:
    """True if newly added, False if they already followed it."""
    cursor = connection.execute(
        "INSERT OR IGNORE INTO subs (did, kind, league, team_id) VALUES (?, ?, ?, NULL)",
        (did, TICKER, league_for(symbol)),
    )
    return bool(cursor.rowcount)


def remove_ticker(connection, did, symbol) -> bool:
    """True if removed, False if they weren't following it."""
    cursor = connection.execute(
        "DELETE FROM subs WHERE did = ? AND kind = ? AND league = ?",
        (did, TICKER, league_for(symbol)),
    )
    return bool(cursor.rowcount)


def ensure_market_tz(connection, did) -> None:
    connection.execute(
        "UPDATE subscribers SET tz = ? WHERE did = ? AND tz IS NULL",
        (MARKET_TZ, did),
    )
