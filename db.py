"""
Enkel databas-hjälpare. Vi använder psycopg2 direkt (ingen ORM) eftersom
inserts är enkla och vi vill ha full kontroll över prestanda senare.
"""
import psycopg2
from psycopg2.extras import Json, execute_values
from contextlib import contextmanager

from config import settings


def get_connection():
    return psycopg2.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
    )


@contextmanager
def get_cursor(commit: bool = True):
    """
    Context manager som ger dig en cursor och sköter commit/rollback/close.
    Användning:
        with get_cursor() as cur:
            cur.execute(...)
    """
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_ohlcv(exchange: str, symbol: str, timeframe: str, candles: list):
    """
    candles: lista av [timestamp_ms, open, high, low, close, volume] (ccxt-format)
    """
    if not candles:
        return
    with get_cursor() as cur:
        values = [
            (exchange, symbol, timeframe, c[0] / 1000.0, c[1], c[2], c[3], c[4], c[5])
            for c in candles
        ]
        execute_values(
            cur,
            """
            INSERT INTO ohlcv (exchange, symbol, timeframe, ts, open, high, low, close, volume)
            VALUES %s
            ON CONFLICT (exchange, symbol, timeframe, ts) DO NOTHING
            """,
            values,
            template="(%s, %s, %s, to_timestamp(%s), %s, %s, %s, %s, %s)",
        )


def insert_orderbook_snapshot(exchange: str, symbol: str, ts, bids, asks, best_bid, best_ask, spread_pct):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO orderbook_snapshots
                (exchange, symbol, ts, bids, asks, best_bid, best_ask, spread_pct)
            VALUES (%s, %s, to_timestamp(%s), %s, %s, %s, %s, %s)
            """,
            (exchange, symbol, ts / 1000.0, Json(bids), Json(asks), best_bid, best_ask, spread_pct),
        )


def insert_trades(exchange: str, symbol: str, trades: list):
    if not trades:
        return
    values = [
        (exchange, symbol, str(t["id"]), t["timestamp"] / 1000.0, t["side"], t["price"], t["amount"])
        for t in trades
        if t.get("id") is not None
    ]
    if not values:
        return
    with get_cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO trades (exchange, symbol, trade_id, ts, side, price, amount)
            VALUES %s
            ON CONFLICT (exchange, symbol, trade_id) DO NOTHING
            """,
            values,
            template="(%s, %s, %s, to_timestamp(%s), %s, %s, %s)",
        )


def insert_funding_rate(exchange: str, symbol: str, ts, funding_rate, next_funding=None):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO funding_rates (exchange, symbol, ts, funding_rate, next_funding)
            VALUES (%s, %s, to_timestamp(%s), %s, to_timestamp(%s))
            ON CONFLICT (exchange, symbol, ts) DO NOTHING
            """,
            (exchange, symbol, ts / 1000.0, funding_rate, next_funding / 1000.0 if next_funding else None),
        )


def insert_open_interest(exchange: str, symbol: str, ts, oi, oi_usd=None):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO open_interest (exchange, symbol, ts, open_interest, open_interest_usd)
            VALUES (%s, %s, to_timestamp(%s), %s, %s)
            ON CONFLICT (exchange, symbol, ts) DO NOTHING
            """,
            (exchange, symbol, ts / 1000.0, oi, oi_usd),
        )


# ---------------------------------------------------------------------------
# Läs-funktioner — används av api.py för att servera data till frontend (Lovable)
# ---------------------------------------------------------------------------

def _rows_to_dicts(cur) -> list[dict]:
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_ohlcv(symbol: str, timeframe: str = "1m", limit: int = 200) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT symbol, timeframe, ts, open, high, low, close, volume
            FROM ohlcv
            WHERE symbol = %s AND timeframe = %s
            ORDER BY ts DESC
            LIMIT %s
            """,
            (symbol, timeframe, limit),
        )
        rows = _rows_to_dicts(cur)
    return list(reversed(rows))  # äldst -> nyast, bra för grafer


def get_latest_orderbook(symbol: str) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT symbol, ts, bids, asks, best_bid, best_ask, spread_pct
            FROM orderbook_snapshots
            WHERE symbol = %s
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol,),
        )
        rows = _rows_to_dicts(cur)
    return rows[0] if rows else None


def get_recent_trades(symbol: str, limit: int = 50) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT symbol, trade_id, ts, side, price, amount
            FROM trades
            WHERE symbol = %s
            ORDER BY ts DESC
            LIMIT %s
            """,
            (symbol, limit),
        )
        rows = _rows_to_dicts(cur)
    return rows


def get_latest_funding_rate(symbol: str) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT symbol, ts, funding_rate, next_funding
            FROM funding_rates
            WHERE symbol = %s
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol,),
        )
        rows = _rows_to_dicts(cur)
    return rows[0] if rows else None


def get_latest_open_interest(symbol: str) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT symbol, ts, open_interest, open_interest_usd
            FROM open_interest
            WHERE symbol = %s
            ORDER BY ts DESC
            LIMIT 1
            """,
            (symbol,),
        )
        rows = _rows_to_dicts(cur)
    return rows[0] if rows else None
