"""
Bot-arena — en bot per strategi, alla handlar samtidigt på samma marknad.

VARFÖR DETTA ÄR BÄTTRE ÄN BARA BACKTESTING:
Backtest visar hur en strategi HADE gått på historisk data, där du redan
vet facit. En bot som handlar framåt i tiden kan inte fuska: den ser samma
marknad som alla andra bottar, i samma sekund, utan att veta vad som
händer härnäst. Det kallas forward testing och är den ärligaste
utvärderingen som finns utan riktiga pengar.

Varje bot har:
  - Egen plånbok (låtsas-USDT)
  - Egna positioner
  - Egen affärshistorik
  - Samma startkapital, så jämförelsen är rättvis

Avgifter och slippage dras av precis som i backtesten, annars blir
topplistan systematiskt för optimistisk.
"""
import json
import logging
from datetime import datetime, timezone

from db import get_cursor, get_ohlcv
import strategies
import momentum_strategy
import orderflow
from backtest import FEE_PCT, SLIPPAGE_PCT

logger = logging.getLogger("bots")

DEFAULT_STARTING_BALANCE = 1000.0
DEFAULT_POSITION_SIZE_PCT = 0.20  # 20% av botens saldo per affär

# Tak för hur många positioner EN bot får hålla samtidigt.
#
# Varför detta behövs: varje bot utvärderar varje bevakad symbol. Med 3
# symboler är det ofarligt, men med 20 kan en bot öppna 20 positioner och
# betala avgift på varje. Det gör den till en indexfond med extra steg —
# och gör jämförelsen mellan bottar meningslös, eftersom en bot som
# handlar allt alltid liknar marknaden.
MAX_POSITIONS_PER_BOT = int(__import__("os").getenv("MAX_POSITIONS_PER_BOT", "4"))


# ---------------------------------------------------------------------------
# Skapa och hantera bottar
# ---------------------------------------------------------------------------

def create_bot(name: str, strategy: str, params: dict,
               starting_balance: float = DEFAULT_STARTING_BALANCE,
               position_size_pct: float = DEFAULT_POSITION_SIZE_PCT) -> dict:
    if strategy not in strategies.STRATEGY_MAP:
        return {"error": f"Okänd strategi: {strategy}"}

    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO bots (name, strategy, params, quote_balance,
                              starting_balance, position_size_pct)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO NOTHING
            RETURNING id
            """,
            (name, strategy, json.dumps(params), starting_balance,
             starting_balance, position_size_pct),
        )
        row = cur.fetchone()
    if row is None:
        return {"error": f"En bot med namnet '{name}' finns redan"}
    return {"id": row[0], "name": name, "strategy": strategy}


def seed_default_bots(starting_balance: float = DEFAULT_STARTING_BALANCE) -> list:
    """
    Skapar en bot per strategi med rimliga standardparametrar.
    Kör en gång — befintliga bottar rörs inte.

    Använder första parameterkombinationen från varje strategis grid.
    Poängen är att jämföra METODER mot varandra, inte att finjustera
    parametrar (det gör labbet).
    """
    created = []
    for cls in strategies.ALL_STRATEGIES:
        combos = strategies.expand_grid(cls)
        # Ta en mittenkombination — mindre risk att hamna på ett extremvärde
        params = combos[len(combos) // 2] if combos else {}
        name = cls.name
        result = create_bot(name, cls.name, params, starting_balance)
        if "error" not in result:
            created.append(result)
    return created


def list_bots() -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT id, name, strategy, params, enabled, quote_balance, "
            "starting_balance, position_size_pct, created_at FROM bots ORDER BY id"
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def set_enabled(bot_id: int, enabled: bool):
    with get_cursor() as cur:
        cur.execute("UPDATE bots SET enabled = %s WHERE id = %s", (enabled, bot_id))


def reset_bot(bot_id: int):
    with get_cursor() as cur:
        cur.execute("DELETE FROM bot_positions WHERE bot_id = %s", (bot_id,))
        cur.execute("DELETE FROM bot_trades WHERE bot_id = %s", (bot_id,))
        cur.execute(
            "UPDATE bots SET quote_balance = starting_balance WHERE id = %s", (bot_id,)
        )


def reset_all_bots():
    with get_cursor() as cur:
        cur.execute("DELETE FROM bot_positions")
        cur.execute("DELETE FROM bot_trades")
        cur.execute("UPDATE bots SET quote_balance = starting_balance")


# ---------------------------------------------------------------------------
# Handel
# ---------------------------------------------------------------------------

def _bot_buy(bot: dict, symbol: str, price: float, reason: str):
    spend = float(bot["quote_balance"]) * float(bot["position_size_pct"])
    if spend < 10:
        return False

    # Slippage + avgift gör att du får färre tokens än det "borde" bli
    effective_price = price * (1 + SLIPPAGE_PCT / 100) * (1 + FEE_PCT / 100)
    amount = spend / effective_price
    fees = spend - (spend / (1 + FEE_PCT / 100))

    with get_cursor() as cur:
        cur.execute(
            "UPDATE bots SET quote_balance = quote_balance - %s WHERE id = %s",
            (spend, bot["id"]),
        )
        cur.execute(
            """
            INSERT INTO bot_positions (bot_id, symbol, amount, entry_price, peak_price)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (bot_id, symbol) DO NOTHING
            """,
            (bot["id"], symbol, amount, effective_price, price),
        )
        cur.execute(
            """
            INSERT INTO bot_trades (bot_id, symbol, side, price, amount,
                                    quote_amount, fees_paid, reason)
            VALUES (%s, %s, 'buy', %s, %s, %s, %s, %s)
            """,
            (bot["id"], symbol, price, amount, spend, fees, reason),
        )
    logger.info("[%s] KÖP %s @ %.8f — %s", bot["name"], symbol, price, reason)
    return True


def _bot_sell(bot: dict, position: dict, price: float, reason: str):
    amount = float(position["amount"])
    entry = float(position["entry_price"])

    effective_price = price * (1 - SLIPPAGE_PCT / 100) * (1 - FEE_PCT / 100)
    proceeds = amount * effective_price
    fees = amount * price - proceeds
    realized_pnl = (effective_price - entry) * amount

    with get_cursor() as cur:
        cur.execute(
            "UPDATE bots SET quote_balance = quote_balance + %s WHERE id = %s",
            (proceeds, bot["id"]),
        )
        cur.execute("DELETE FROM bot_positions WHERE id = %s", (position["id"],))
        cur.execute(
            """
            INSERT INTO bot_trades (bot_id, symbol, side, price, amount,
                                    quote_amount, realized_pnl, fees_paid, reason)
            VALUES (%s, %s, 'sell', %s, %s, %s, %s, %s, %s)
            """,
            (bot["id"], position["symbol"], price, amount, proceeds,
             realized_pnl, fees, reason),
        )
    logger.info("[%s] SÄLJ %s @ %.8f — %s (PnL %.2f)",
                bot["name"], position["symbol"], price, reason, realized_pnl)
    return realized_pnl


def _get_positions(bot_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT id, bot_id, symbol, amount, entry_price, peak_price, opened_at "
            "FROM bot_positions WHERE bot_id = %s",
            (bot_id,),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def run_all_bots(symbols: list[str], timeframe: str = "1m"):
    """
    Kör en cykel för alla aktiva bottar.

    EFFEKTIVITET: candles hämtas EN gång per symbol och delas av alla
    bottar. Med 9 bottar × 3 symboler blir det 3 databasfrågor istället
    för 27.
    """
    bots = [b for b in list_bots() if b["enabled"]]
    if not bots:
        return

    # Hämta data en gång
    candle_cache = {}
    for sym in symbols:
        rows = get_ohlcv(sym, timeframe, limit=200)
        if len(rows) >= 60:
            # Gör om till ccxt-format som strategierna förväntar sig
            candle_cache[sym] = [
                [int(r["ts"].timestamp() * 1000), float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"]), float(r["volume"])]
                for r in rows
            ]

    if not candle_cache:
        logger.info("Ingen candle-data tillgänglig än för bottarna")
        return

    # Order flow räknas ut EN gång per symbol och delas av alla bottar
    # som behöver den. Annars skulle varje bot göra samma tunga
    # databasfrågor om och om igen.
    needs_flow = any(
        strategies.STRATEGY_MAP.get(b["strategy"]) is not None
        and strategies.STRATEGY_MAP[b["strategy"]].needs_context
        for b in bots
    )
    flow_cache = {}
    regime_cache = {}
    hype_cache = {}
    if needs_flow:
        import regime as regime_mod
        import social
        for sym, candles in candle_cache.items():
            try:
                flow_cache[sym] = orderflow.get_flow_metrics(sym, window_minutes=15)
            except Exception as e:
                logger.error("Order flow misslyckades för %s: %s", sym, e)
            try:
                regime_cache[sym] = regime_mod.detect_regime(candles)
            except Exception as e:
                logger.error("Regimdetektering misslyckades för %s: %s", sym, e)
            try:
                hype_cache[sym] = social.hype_score(sym)
            except Exception as e:
                logger.debug("Hype-hämtning misslyckades för %s: %s", sym, e)

    now = datetime.now(timezone.utc)

    for bot in bots:
        cls = strategies.STRATEGY_MAP.get(bot["strategy"])
        if cls is None:
            continue

        params = bot["params"] if isinstance(bot["params"], dict) else json.loads(bot["params"])
        positions = {p["symbol"]: p for p in _get_positions(bot["id"])}

        for symbol, candles in candle_cache.items():
            try:
                strat = cls(**params)
                if cls.needs_context:
                    if cls.name == "ensemble_ai":
                        strat.context = {
                            "symbol": symbol,
                            "flow": flow_cache.get(symbol),
                            "regime": regime_cache.get(symbol),
                            "hype": hype_cache.get(symbol),
                        }
                    else:
                        strat.context = flow_cache.get(symbol)
                strat.prepare(candles)
                i = len(candles) - 1
                sig = strat.signal(i)
                price = candles[i][4]

                pos = positions.get(symbol)

                if pos is None:
                    if sig == "buy":
                        # Tak för antal samtidiga positioner
                        if len(positions) >= MAX_POSITIONS_PER_BOT:
                            continue
                        # Ladda om saldot — tidigare köp i samma cykel kan ha ändrat det
                        fresh = next((b for b in list_bots() if b["id"] == bot["id"]), bot)
                        if _bot_buy(fresh, symbol, price, f"{strat.describe()} signal"):
                            # Håll räkningen aktuell inom samma cykel
                            positions[symbol] = {"symbol": symbol}
                else:
                    # Uppdatera topp för trailing stop
                    peak = max(float(pos["peak_price"] or 0), price)
                    with get_cursor() as cur:
                        cur.execute(
                            "UPDATE bot_positions SET peak_price = %s WHERE id = %s",
                            (peak, pos["id"]),
                        )

                    exit_now, reason = False, ""
                    if cls.exit_mode == "signal":
                        if sig == "sell":
                            exit_now, reason = True, "SELL-signal"
                    else:
                        held_min = (now - pos["opened_at"]).total_seconds() / 60
                        exit_now, reason = momentum_strategy.check_exit(
                            {"avg_entry_price": float(pos["entry_price"]), "peak_price": peak},
                            price, held_min,
                        )

                    if exit_now:
                        fresh = next((b for b in list_bots() if b["id"] == bot["id"]), bot)
                        _bot_sell(fresh, pos, price, reason)

            except Exception as e:
                logger.error("Bot %s fel på %s: %s", bot["name"], symbol, e)


# ---------------------------------------------------------------------------
# Statistik och topplista
# ---------------------------------------------------------------------------

def bot_stats(bot_id: int) -> dict:
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT id, name, strategy, params, enabled, quote_balance, "
            "starting_balance, created_at FROM bots WHERE id = %s",
            (bot_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": "Bot finns inte"}
        cols = [c.name for c in cur.description]
        bot = dict(zip(cols, row))

        cur.execute(
            "SELECT side, realized_pnl, fees_paid, ts FROM bot_trades "
            "WHERE bot_id = %s ORDER BY ts",
            (bot_id,),
        )
        trades = cur.fetchall()

    positions = _get_positions(bot_id)

    # Nuvarande värde på öppna positioner
    open_value = 0.0
    open_detail = []
    for p in positions:
        latest = get_ohlcv(p["symbol"], "1m", limit=1)
        current = float(latest[-1]["close"]) if latest else float(p["entry_price"])
        value = current * float(p["amount"])
        open_value += value
        open_detail.append({
            "symbol": p["symbol"],
            "entry_price": float(p["entry_price"]),
            "current_price": current,
            "value_usdt": round(value, 2),
            "unrealized_pnl": round((current - float(p["entry_price"])) * float(p["amount"]), 2),
            "opened_at": p["opened_at"],
        })

    starting = float(bot["starting_balance"])
    total_value = float(bot["quote_balance"]) + open_value

    sells = [t for t in trades if t[0] == "sell" and t[1] is not None]
    pnls = [float(t[1]) for t in sells]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_fees = sum(float(t[2] or 0) for t in trades)

    # Drawdown från kumulativ realiserad resultatkurva
    equity = starting
    peak = starting
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        if peak:
            max_dd = max(max_dd, (peak - equity) / peak * 100)

    days_running = max((datetime.now(timezone.utc) - bot["created_at"]).total_seconds() / 86400, 0.01)

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "id": bot["id"],
        "name": bot["name"],
        "strategy": bot["strategy"],
        "params": bot["params"],
        "enabled": bot["enabled"],
        "starting_balance": starting,
        "quote_balance": round(float(bot["quote_balance"]), 2),
        "open_positions_value": round(open_value, 2),
        "total_value": round(total_value, 2),
        "return_pct": round((total_value - starting) / starting * 100, 2),
        "closed_trades": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(sells) * 100, 1) if sells else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(max_dd, 2),
        "total_fees_paid": round(total_fees, 2),
        "open_positions": open_detail,
        "days_running": round(days_running, 2),
        "trades_per_day": round(len(sells) / days_running, 1),
        # Ärlighetsmätare: hur mycket kan man lita på siffrorna än?
        "confidence": _confidence(len(sells), days_running),
    }


def _confidence(closed_trades: int, days: float) -> dict:
    """
    Säger rakt ut hur mycket siffrorna är värda än.

    Det här fältet finns för att en topplista efter två dagar frestar
    till slutsatser som datan inte bär. Att veta att man inte vet är
    hela poängen med forward testing.
    """
    if closed_trades < 10:
        level = "ingen"
        note = f"Bara {closed_trades} avslutade affärer. Detta är brus, inte resultat."
    elif closed_trades < 30:
        level = "låg"
        note = f"{closed_trades} affärer. Börjar bli intressant men enskilda affärer dominerar fortfarande."
    elif closed_trades < 100:
        level = "medel"
        note = f"{closed_trades} affärer. Rimligt underlag, men marknadsläget kan ha gynnat en viss stil."
    else:
        level = "hyfsad"
        note = f"{closed_trades} affärer. Statistiskt användbart — men bekräfta över olika marknadslägen."

    if days < 3:
        note += f" Boten har bara kört i {days:.1f} dagar — alla har sett samma marknad."

    return {"level": level, "note": note}


def leaderboard() -> dict:
    """Alla bottar rankade, med ärlig kontext."""
    bots = list_bots()
    stats = [bot_stats(b["id"]) for b in bots]
    stats = [s for s in stats if "error" not in s]
    stats.sort(key=lambda s: s["return_pct"], reverse=True)

    baseline = next((s for s in stats if s["strategy"] == "buy_and_hold"), None)
    total_trades = sum(s["closed_trades"] for s in stats)
    max_days = max((s["days_running"] for s in stats), default=0)

    beat_baseline = []
    if baseline:
        beat_baseline = [
            s["name"] for s in stats
            if s["strategy"] != "buy_and_hold" and s["return_pct"] > baseline["return_pct"]
        ]

    return {
        "bots": stats,
        "summary": {
            "bots_running": len([s for s in stats if s["enabled"]]),
            "total_closed_trades": total_trades,
            "days_running": round(max_days, 2),
            "baseline_buy_and_hold_pct": baseline["return_pct"] if baseline else None,
            "beating_baseline": beat_baseline,
            "verdict": _leaderboard_verdict(stats, baseline, total_trades, max_days),
        },
    }


def _leaderboard_verdict(stats, baseline, total_trades, days) -> str:
    if not stats:
        return "Inga bottar skapade än. Kör POST /api/bots/seed för att starta arenan."

    if total_trades < 20:
        return (f"Bara {total_trades} avslutade affärer totalt efter {days:.1f} dagar. "
                "Topplistan säger ingenting än — låt den gå minst ett par veckor.")

    real = [s for s in stats if s["strategy"] != "buy_and_hold"]
    if not real:
        return "Bara referensboten kör."

    best = real[0]
    if baseline and best["return_pct"] <= baseline["return_pct"]:
        return (f"Ingen strategi slår buy & hold ({baseline['return_pct']}%) än. "
                "Det är det vanligaste utfallet och inget att skämmas för — "
                "det betyder bara att komplexiteten inte lönar sig i detta marknadsläge.")

    if best["closed_trades"] < 30:
        return (f"{best['name']} leder med {best['return_pct']}%, men på bara "
                f"{best['closed_trades']} affärer. För tidigt att lita på.")

    return (f"{best['name']} leder med {best['return_pct']}% på {best['closed_trades']} affärer "
            f"efter {days:.1f} dagar. Lovande — men kolla max_drawdown_pct innan du blir förtjust, "
            "och låt det gå över fler marknadslägen.")
