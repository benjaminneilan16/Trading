"""
Paper Trading Engine — simulerad Fas 2 (Execution) + Fas 9 (Portfolio).

Handlar med LÅTSASPENGAR men mot RIKTIGA priser (senaste stängningspris
från OHLCV-datan). Ingen riktig order läggs någonstans, inga riktiga
pengar rör sig — bara loggning i databasen så du kan se hur strategin
hade presterat.

Enkel riskhantering inbyggd redan nu (mer avancerat kommer i Fas 3):
- Max en öppen position per symbol åt gången (ingen "dubbelköp")
- Satsar en fast andel av tillgängligt saldo per köp (POSITION_SIZE_PCT)
"""
import logging
from db import get_cursor, get_ohlcv

logger = logging.getLogger("paper_trading")

POSITION_SIZE_PCT = 0.10  # satsa 10% av tillgängligt låtsassaldo per köp
DEFAULT_STARTING_BALANCE = 10_000.0  # låtsas-USDT att börja med


def ensure_wallet_exists(starting_balance: float = DEFAULT_STARTING_BALANCE):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO paper_wallet (id, quote_currency, quote_balance, starting_balance)
            VALUES (1, 'USDT', %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (starting_balance, starting_balance),
        )


def get_portfolio() -> dict:
    """Hela läget: saldo, öppna positioner, och totalt värde just nu."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT quote_currency, quote_balance, starting_balance FROM paper_wallet WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            return {"error": "Plånboken är inte initierad än"}
        quote_currency, quote_balance, starting_balance = row

        cur.execute("SELECT symbol, amount, avg_entry_price, opened_at FROM paper_positions")
        positions = []
        cols = [c.name for c in cur.description]
        for r in cur.fetchall():
            positions.append(dict(zip(cols, r)))

    # Räkna ut nuvarande värde på varje position med senaste pris
    total_position_value = 0.0
    for pos in positions:
        latest = get_ohlcv(pos["symbol"], "1m", limit=1)
        current_price = float(latest[-1]["close"]) if latest else float(pos["avg_entry_price"])
        pos["current_price"] = current_price
        pos["value_usdt"] = current_price * float(pos["amount"])
        pos["unrealized_pnl"] = (current_price - float(pos["avg_entry_price"])) * float(pos["amount"])
        total_position_value += pos["value_usdt"]

    total_value = float(quote_balance) + total_position_value

    return {
        "quote_currency": quote_currency,
        "quote_balance": float(quote_balance),
        "starting_balance": float(starting_balance),
        "positions": positions,
        "total_value": total_value,
        "total_pnl": total_value - float(starting_balance),
        "total_pnl_pct": (total_value - float(starting_balance)) / float(starting_balance) * 100,
    }


def has_open_position(symbol: str) -> bool:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM paper_positions WHERE symbol = %s", (symbol,))
        return cur.fetchone() is not None


def buy(symbol: str, price: float, reason: str = "") -> dict:
    with get_cursor() as cur:
        cur.execute("SELECT quote_balance FROM paper_wallet WHERE id = 1 FOR UPDATE")
        (balance,) = cur.fetchone()
        balance = float(balance)

        spend = balance * POSITION_SIZE_PCT
        if spend < 1 or spend > balance:
            return {"executed": False, "reason": "otillräckligt saldo"}

        amount = spend / price

        cur.execute(
            "UPDATE paper_wallet SET quote_balance = quote_balance - %s, updated_at = now() WHERE id = 1",
            (spend,),
        )
        cur.execute(
            """
            INSERT INTO paper_positions (symbol, amount, avg_entry_price)
            VALUES (%s, %s, %s)
            ON CONFLICT (symbol) DO UPDATE SET
                amount = paper_positions.amount + EXCLUDED.amount,
                avg_entry_price = (
                    (paper_positions.amount * paper_positions.avg_entry_price)
                    + (EXCLUDED.amount * EXCLUDED.avg_entry_price)
                ) / (paper_positions.amount + EXCLUDED.amount)
            """,
            (symbol, amount, price),
        )
        cur.execute(
            """
            INSERT INTO paper_trades (symbol, side, price, amount, quote_amount, reason)
            VALUES (%s, 'buy', %s, %s, %s, %s)
            """,
            (symbol, price, amount, spend, reason),
        )
    logger.info("PAPER BUY %s: %.6f @ %.2f (spend %.2f USDT) — %s", symbol, amount, price, spend, reason)
    return {"executed": True, "side": "buy", "symbol": symbol, "amount": amount, "price": price}


def sell(symbol: str, price: float, reason: str = "") -> dict:
    with get_cursor() as cur:
        cur.execute(
            "SELECT amount, avg_entry_price FROM paper_positions WHERE symbol = %s FOR UPDATE",
            (symbol,),
        )
        row = cur.fetchone()
        if row is None:
            return {"executed": False, "reason": "ingen öppen position att sälja"}

        amount, avg_entry_price = float(row[0]), float(row[1])
        proceeds = amount * price
        realized_pnl = (price - avg_entry_price) * amount

        cur.execute(
            "UPDATE paper_wallet SET quote_balance = quote_balance + %s, updated_at = now() WHERE id = 1",
            (proceeds,),
        )
        cur.execute("DELETE FROM paper_positions WHERE symbol = %s", (symbol,))
        cur.execute(
            """
            INSERT INTO paper_trades (symbol, side, price, amount, quote_amount, realized_pnl, reason)
            VALUES (%s, 'sell', %s, %s, %s, %s, %s)
            """,
            (symbol, price, amount, proceeds, realized_pnl, reason),
        )
    logger.info(
        "PAPER SELL %s: %.6f @ %.2f (proceeds %.2f USDT, PnL %.2f) — %s",
        symbol, amount, price, proceeds, realized_pnl, reason,
    )
    return {"executed": True, "side": "sell", "symbol": symbol, "amount": amount, "price": price, "realized_pnl": realized_pnl}


def log_signal(symbol: str, decision_data: dict):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO strategy_signals
                (symbol, decision, score, ema_fast, ema_slow, rsi, macd, macd_signal, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                symbol,
                decision_data.get("decision"),
                decision_data.get("score"),
                decision_data.get("ema_fast"),
                decision_data.get("ema_slow"),
                decision_data.get("rsi"),
                decision_data.get("macd"),
                decision_data.get("macd_signal"),
                decision_data.get("reason"),
            ),
        )


def reset_wallet(starting_balance: float = DEFAULT_STARTING_BALANCE):
    """Nollställer papperskontot helt — nytt saldo, inga positioner, historiken i trades/signals bevaras."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM paper_positions")
        cur.execute(
            """
            INSERT INTO paper_wallet (id, quote_currency, quote_balance, starting_balance)
            VALUES (1, 'USDT', %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                quote_balance = EXCLUDED.quote_balance,
                starting_balance = EXCLUDED.starting_balance,
                updated_at = now()
            """,
            (starting_balance, starting_balance),
        )


def get_recent_trades(limit: int = 50) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT symbol, side, price, amount, quote_amount, realized_pnl, reason, ts "
            "FROM paper_trades ORDER BY ts DESC LIMIT %s",
            (limit,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_recent_signals(symbol: str = None, limit: int = 20) -> list[dict]:
    with get_cursor(commit=False) as cur:
        if symbol:
            cur.execute(
                "SELECT * FROM strategy_signals WHERE symbol = %s ORDER BY ts DESC LIMIT %s",
                (symbol, limit),
            )
        else:
            cur.execute("SELECT * FROM strategy_signals ORDER BY ts DESC LIMIT %s", (limit,))
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Momentum-strategin — snabba in/ut-positioner
# ---------------------------------------------------------------------------

def buy_momentum(symbol: str, price: float, size_pct: float, reason: str = "") -> dict:
    """
    Som buy(), men taggar positionen som 'momentum' och sätter peak_price
    direkt så trailing stop kan börja jobba från första sekunden.
    """
    with get_cursor() as cur:
        cur.execute("SELECT quote_balance FROM paper_wallet WHERE id = 1 FOR UPDATE")
        (balance,) = cur.fetchone()
        balance = float(balance)

        spend = balance * size_pct
        if spend < 1 or spend > balance:
            return {"executed": False, "reason": "otillräckligt saldo"}

        amount = spend / price

        cur.execute(
            "UPDATE paper_wallet SET quote_balance = quote_balance - %s, updated_at = now() WHERE id = 1",
            (spend,),
        )
        cur.execute(
            """
            INSERT INTO paper_positions (symbol, amount, avg_entry_price, peak_price, strategy)
            VALUES (%s, %s, %s, %s, 'momentum')
            ON CONFLICT (symbol) DO NOTHING
            """,
            (symbol, amount, price, price),
        )
        cur.execute(
            """
            INSERT INTO paper_trades (symbol, side, price, amount, quote_amount, reason)
            VALUES (%s, 'buy', %s, %s, %s, %s)
            """,
            (symbol, price, amount, spend, reason),
        )
    logger.info("MOMENTUM BUY %s: %.6f @ %.8f (%.2f USDT) — %s", symbol, amount, price, spend, reason)
    return {"executed": True, "side": "buy", "symbol": symbol, "amount": amount, "price": price}


def get_open_positions(strategy: str = None) -> list[dict]:
    with get_cursor(commit=False) as cur:
        if strategy:
            cur.execute(
                "SELECT symbol, amount, avg_entry_price, peak_price, opened_at, strategy "
                "FROM paper_positions WHERE strategy = %s",
                (strategy,),
            )
        else:
            cur.execute(
                "SELECT symbol, amount, avg_entry_price, peak_price, opened_at, strategy "
                "FROM paper_positions"
            )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def update_peak_price(symbol: str, price: float):
    """Höjer peak_price om priset satt ny topp — grunden för trailing stop."""
    with get_cursor() as cur:
        cur.execute(
            "UPDATE paper_positions SET peak_price = GREATEST(COALESCE(peak_price, 0), %s) "
            "WHERE symbol = %s",
            (price, symbol),
        )


def count_open_positions(strategy: str = None) -> int:
    with get_cursor(commit=False) as cur:
        if strategy:
            cur.execute("SELECT COUNT(*) FROM paper_positions WHERE strategy = %s", (strategy,))
        else:
            cur.execute("SELECT COUNT(*) FROM paper_positions")
        return cur.fetchone()[0]


def log_scanner_hit(hit: dict, entered: bool):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO scanner_hits
                (symbol, score, volume_ratio, price_change_15m_pct,
                 quote_volume_24h, spread_pct, accelerating, entered, reason)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                hit["symbol"], hit["score"], hit.get("volume_ratio"),
                hit.get("price_change_15m_pct"), hit.get("quote_volume_24h"),
                hit.get("spread_pct"), hit.get("accelerating"), entered, hit.get("reason"),
            ),
        )


def get_scanner_hits(limit: int = 50) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT symbol, ts, score, volume_ratio, price_change_15m_pct, "
            "quote_volume_24h, spread_pct, accelerating, entered, reason "
            "FROM scanner_hits ORDER BY ts DESC LIMIT %s",
            (limit,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
