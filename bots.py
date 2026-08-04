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
from notifier import send_notification

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

# --- Säkerhetsnät som gäller ALLA bottar, oavsett strategi ---
#
# Varför detta behövs: strategier med exit_mode "signal" säljer bara när
# strategin ger säljsignal. Kommer den aldrig rider positionen ner hur
# långt som helst. En RSI-bot som köper vid 25 och token faller 70% sitter
# kvar för alltid.
#
# Utan detta mäter arenan inte vilken strategi som är bäst, utan vilken
# som hade turen att slippa en katastrof.
#
# Nivåerna är medvetet vida — de ska fånga haverier, inte ersätta
# strategiernas egna exits.
HARD_STOP_LOSS_PCT = float(__import__("os").getenv("BOT_HARD_STOP_PCT", "-15.0"))
MAX_HOLD_HOURS = float(__import__("os").getenv("BOT_MAX_HOLD_HOURS", "72"))

# Handla inte på data som är äldre än så här (minuter). Slutar
# insamlingen fungera fryser sista candlen, och utan denna koll skulle
# bottarna fortsätta handla på ett pris som inte finns längre.
MAX_CANDLE_AGE_MINUTES = float(__import__("os").getenv("MAX_CANDLE_AGE_MINUTES", "15"))

# ---------------------------------------------------------------------------
# Tidsskala för arenan
#
# Bakgrund: den första mätperioden gav 2 694 affärer på två dygn och
# 1 181 USDT i avgifter. Median-hålltiden var 10-22 minuter på flera
# bottar, och 62% av macd_cross affärer varade under 15 minuter.
#
# Orsaken var att vi utvärderade strategier byggda för timmar och dagar
# på 1-minuterscandles, varje minut. Signalen pendlar då kring tröskeln
# och triggar köp och sälj om vartannat.
#
# Lika illa: whale_follow stängdes av TIME EXIT i 210 fall av 210. Inte
# en enda position fick utvecklas färdigt. Vi mätte inte strategierna —
# vi mätte vår egen tidsgräns.
# ---------------------------------------------------------------------------

# Vilken candle-upplösning strategierna ser
ARENA_TIMEFRAME_MINUTES = int(__import__("os").getenv("ARENA_TIMEFRAME_MINUTES", "5"))

# Ingen exit (utom stop loss) före denna tid
ARENA_MIN_HOLD_MINUTES = float(__import__("os").getenv("ARENA_MIN_HOLD_MINUTES", "30"))

# Regelbaserade exits: hur länge en position får utvecklas.
# Höjd från 45 minuter till 12 timmar — ett Donchian-utbrott är byggt för
# att spela ut över dagar, inte under en lunchrast.
ARENA_MAX_HOLD_MINUTES = float(__import__("os").getenv("ARENA_MAX_HOLD_MINUTES", "720"))

# Vinstmål och trailing stop för arenans regelbaserade exits.
# Vidare än momentum-scannerns, eftersom vi nu jagar större rörelser.
ARENA_TAKE_PROFIT_PCT = float(__import__("os").getenv("ARENA_TAKE_PROFIT_PCT", "6.0"))
ARENA_TRAILING_STOP_PCT = float(__import__("os").getenv("ARENA_TRAILING_STOP_PCT", "3.0"))

# Hur många cykler i rad samma signal måste synas innan den agerar på.
# Filtrerar bort signaler som bara nuddar tröskeln och studsar tillbaka.
SIGNAL_CONFIRMATIONS = int(__import__("os").getenv("SIGNAL_CONFIRMATIONS", "2"))

# Minne av föregående signaler: {(bot_id, symbol): [signal, signal, ...]}
_signal_history: dict = {}


def _confirmed_signal(bot_id: int, symbol: str, signal: str) -> str:
    """
    Returnerar signalen bara om den upprepats tillräckligt många cykler.
    Annars "hold".

    Utan detta agerar boten på varje litet hopp över tröskeln. Med två
    bekräftelser krävs att signalen håller i sig — vilket sållar bort
    en stor del av brusaffärerna.
    """
    # Med dynamisk bevakningslista dyker nya symboler upp hela tiden.
    # Utan tak skulle minnet växa obegränsat under veckor av drift.
    if len(_signal_history) > 5000:
        _signal_history.clear()

    key = (bot_id, symbol)
    history = _signal_history.get(key, [])
    history.append(signal)
    history = history[-SIGNAL_CONFIRMATIONS:]
    _signal_history[key] = history

    if len(history) < SIGNAL_CONFIRMATIONS:
        return "hold"
    if all(s == signal for s in history):
        return signal
    return "hold"


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

def effective_slippage(symbol: str, tickers: dict) -> float:
    """
    Slippage baserad på faktisk spread istället för en fast siffra.

    VARFÖR: 0,15% fast slippage är rimligt för BTC men grovt optimistiskt
    för en småtoken med 0,8% spread. Att korsa spreaden kostar minst
    halva den — och när bottarna nu jagar småtokens skulle en fast siffra
    göra resultaten systematiskt för positiva.

    Att överskatta sina kostnader är billigt. Att underskatta dem betyder
    att en strategi ser lönsam ut på papper och förlorar pengar på riktigt.
    """
    t = tickers.get(symbol) or {}
    bid, ask = t.get("bid"), t.get("ask")
    if bid and ask and bid > 0:
        half_spread = (ask - bid) / bid * 100 / 2
        return max(SLIPPAGE_PCT, half_spread)
    return SLIPPAGE_PCT


def _bot_buy(bot: dict, symbol: str, price: float, reason: str,
             slippage_pct: float = None):
    spend = float(bot["quote_balance"]) * float(bot["position_size_pct"])
    if spend < 10:
        return False

    slip = SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    # Slippage + avgift gör att du får färre tokens än det "borde" bli
    effective_price = price * (1 + slip / 100) * (1 + FEE_PCT / 100)
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
    _cycle_events.append({
        "type": "buy", "bot": bot["name"], "symbol": symbol,
        "price": price, "reason": reason,
    })
    return True


def _bot_sell(bot: dict, position: dict, price: float, reason: str,
              slippage_pct: float = None):
    amount = float(position["amount"])
    entry = float(position["entry_price"])

    slip = SLIPPAGE_PCT if slippage_pct is None else slippage_pct
    effective_price = price * (1 - slip / 100) * (1 - FEE_PCT / 100)
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
    _cycle_events.append({
        "type": "sell", "bot": bot["name"], "symbol": position["symbol"],
        "price": price, "reason": reason, "pnl": realized_pnl,
    })
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


# ---------------------------------------------------------------------------
# Dynamisk bevakningslista — låter bottarna jaga småtokens
# ---------------------------------------------------------------------------

# Hur många dynamiska kandidater som tas in per cykel utöver de fasta
MAX_DYNAMIC_SYMBOLS = int(__import__("os").getenv("MAX_DYNAMIC_SYMBOLS", "15"))
DYNAMIC_WATCHLIST_ENABLED = __import__("os").getenv(
    "DYNAMIC_WATCHLIST_ENABLED", "true").lower() in ("1", "true", "yes")

# Notisläge för arenan:
#   "all"   — köp och sälj
#   "sells" — bara avslutade affärer (resultatet är det intressanta)
#   "off"   — inga notiser
#
# Notiserna samlas ihop per cykel och skickas som ETT meddelande. Tolv
# bottar som handlar samtidigt skulle annars ge tio separata notiser inom
# några sekunder, och då slutar man läsa dem.
BOT_NOTIFICATIONS = __import__("os").getenv("BOT_NOTIFICATIONS", "all").lower()
# Hur länge kandidatlistan återanvänds (sekunder). Att bygga den kostar
# ett API-anrop per symbol, så vi gör det inte varje botcykel.
WATCHLIST_TTL_SECONDS = 300

_watchlist_cache = {"built_at": 0.0, "candles": {}, "tickers": {}}


def build_dynamic_watchlist(exchange) -> tuple[dict, dict]:
    """
    Bygger en lista över småtokens som rör sig just nu, och hämtar candles
    för dem. Returnerar (candles_per_symbol, alla_tickers).

    Urvalet är medvetet enklare än momentum-scannerns: vi vill ge bottarna
    ett RIMLIGT urval att välja bland, inte förhandsvälja åt dem. Skulle vi
    filtrera hårt här mätte vi scannerns urval istället för strategierna.
    """
    import time
    now = time.time()

    if now - _watchlist_cache["built_at"] < WATCHLIST_TTL_SECONDS and _watchlist_cache["candles"]:
        return _watchlist_cache["candles"], _watchlist_cache["tickers"]

    try:
        from collectors.exchange import fetch_all_tickers
        tickers = fetch_all_tickers(exchange)
    except Exception as e:
        logger.error("Kunde inte hämta tickers för bevakningslistan: %s", e)
        return _watchlist_cache["candles"], _watchlist_cache["tickers"]

    import scanner as sc

    candidates = []
    for symbol, t in tickers.items():
        if not symbol.endswith("/USDT"):
            continue
        qv = t.get("quoteVolume")
        bid, ask = t.get("bid"), t.get("ask")
        if not qv or not bid or not ask or bid <= 0:
            continue
        if not (sc.MIN_24H_QUOTE_VOLUME <= qv <= sc.MAX_24H_QUOTE_VOLUME):
            continue
        spread_pct = (ask - bid) / bid * 100
        if spread_pct > sc.MAX_SPREAD_PCT:
            continue

        # Rankning: dagsrörelse, med bonus för unga tokens
        score = abs(t.get("percentage") or 0)
        try:
            import newlistings
            bonus, _ = newlistings.newness_bonus(symbol)
            score += bonus * 5  # ungdom väger tungt i URVALET, inte i beslutet
        except Exception:
            pass

        candidates.append((symbol, score))

    candidates.sort(key=lambda c: c[1], reverse=True)
    chosen = [c[0] for c in candidates[:MAX_DYNAMIC_SYMBOLS]]

    candles = {}
    for sym in chosen:
        try:
            raw = exchange.fetch_ohlcv(
                sym, timeframe=f"{ARENA_TIMEFRAME_MINUTES}m", limit=200)
            if len(raw) >= 60:
                candles[sym] = raw
        except Exception as e:
            logger.debug("Kunde inte hämta candles för %s: %s", sym, e)

    _watchlist_cache.update({"built_at": now, "candles": candles, "tickers": tickers})
    logger.info("Dynamisk bevakningslista: %d symboler (%s)",
                len(candles), ", ".join(list(candles)[:5]))
    return candles, tickers


# Buffert för händelser under en cykel, töms av _flush_notifications()
_cycle_events: list[dict] = []


def _flush_notifications():
    """
    Skickar ETT samlat meddelande för allt som hänt under cykeln.

    Säkerhetsstopp och maxtid-exits lyfts fram separat, eftersom de betyder
    att strategins egen logik inte räckte till — det är viktigare
    information än en vanlig affär.
    """
    global _cycle_events
    events, _cycle_events = _cycle_events, []

    if not events or BOT_NOTIFICATIONS == "off":
        return

    buys = [e for e in events if e["type"] == "buy"]
    sells = [e for e in events if e["type"] == "sell"]

    if BOT_NOTIFICATIONS == "sells":
        buys = []

    if not buys and not sells:
        return

    lines = []

    if sells:
        total_pnl = sum(e.get("pnl") or 0 for e in sells)
        emoji = "✅" if total_pnl >= 0 else "🔻"
        lines.append(f"{emoji} {len(sells)} avslut · {total_pnl:+.2f} USDT")
        for e in sells[:8]:
            pnl = e.get("pnl") or 0
            mark = "🟢" if pnl >= 0 else "🔴"
            # Säkerhetsnätet får en egen markering
            if "SÄKERHETSSTOPP" in e["reason"] or "MAXTID" in e["reason"]:
                mark = "⚠️"
            lines.append(f"{mark} {e['bot']} · {e['symbol']} · {pnl:+.2f}")
            lines.append(f"   {e['reason'][:70]}")
        if len(sells) > 8:
            lines.append(f"   (+{len(sells) - 8} till)")

    if buys:
        if lines:
            lines.append("")
        lines.append(f"🟢 {len(buys)} nya köp")
        for e in buys[:8]:
            lines.append(f"· {e['bot']} · {e['symbol']} @ {e['price']:.6f}")
        if len(buys) > 8:
            lines.append(f"   (+{len(buys) - 8} till)")

    try:
        send_notification("\n".join(lines))
    except Exception as e:
        logger.error("Kunde inte skicka botnotis: %s", e)


def run_all_bots(symbols: list[str], timeframe: str = "1m", exchange=None):
    """
    Kör en cykel för alla aktiva bottar.

    EFFEKTIVITET: candles hämtas EN gång per symbol och delas av alla
    bottar. Med 9 bottar × 3 symboler blir det 3 databasfrågor istället
    för 27.
    """
    bots = [b for b in list_bots() if b["enabled"]]
    if not bots:
        return

    # Kill switch gäller ALLA bottar, inte bara momentum-scannern.
    # Befintliga positioner får ligga kvar och stängas av sina vanliga
    # regler — men inga nya köp.
    try:
        import risk_manager
        risk_state = risk_manager.get_risk_state()
        kill_switch_on = risk_state["kill_switch_active"]
        if kill_switch_on:
            logger.warning("Kill switch aktiv — bottarna gör inga nya köp")
    except Exception as e:
        logger.error("Kunde inte läsa riskläge: %s", e)
        kill_switch_on = False

    # Hämta data en gång
    candle_cache = {}
    stale_symbols = []
    now_check = datetime.now(timezone.utc)

    from db import get_ohlcv_resampled
    for sym in symbols:
        rows = get_ohlcv_resampled(sym, ARENA_TIMEFRAME_MINUTES, limit=200)
        if len(rows) < 60:
            continue

        # Färskhetskoll — handla inte på frusen data
        age_minutes = (now_check - rows[-1]["ts"]).total_seconds() / 60
        # En 5-minuterscandle är per definition upp till 5 min "gammal"
        if age_minutes > MAX_CANDLE_AGE_MINUTES + ARENA_TIMEFRAME_MINUTES:
            stale_symbols.append((sym, round(age_minutes, 1)))
            continue

        candle_cache[sym] = [
            [int(r["ts"].timestamp() * 1000), float(r["open"]), float(r["high"]),
             float(r["low"]), float(r["close"]), float(r["volume"])]
            for r in rows
        ]

    if stale_symbols:
        logger.warning(
            "Hoppar över inaktuell data: %s",
            ", ".join(f"{s} ({a} min gammal)" for s, a in stale_symbols),
        )

    # --- Dynamiska kandidater: småtokens som rör sig just nu ---
    # De fasta symbolerna kommer från databasen (redan insamlade).
    # De dynamiska hämtas direkt från börsen, eftersom vi inte samlar in
    # data för hundratals par.
    fixed_symbols = set(candle_cache.keys())
    all_tickers = {}

    if exchange is not None and DYNAMIC_WATCHLIST_ENABLED:
        try:
            dynamic_candles, all_tickers = build_dynamic_watchlist(exchange)
            for sym, raw in dynamic_candles.items():
                if sym not in candle_cache:
                    candle_cache[sym] = raw
        except Exception as e:
            logger.error("Dynamisk bevakningslista misslyckades: %s", e)

    # --- Priser för ÖPPNA positioner som inte finns i listan ---
    #
    # Kritiskt: en bot som köpt en småtoken måste kunna sälja den även när
    # token åkt ur kandidatlistan. Utan detta fastnar positioner för alltid
    # och säkerhetsnätet kan aldrig lösa ut.
    orphan_prices = {}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT DISTINCT symbol FROM bot_positions")
        held_symbols = {r[0] for r in cur.fetchall()}

    orphans = held_symbols - set(candle_cache.keys())
    for sym in orphans:
        price = None
        if all_tickers.get(sym, {}).get("last"):
            price = all_tickers[sym]["last"]
        elif exchange is not None:
            try:
                price = exchange.fetch_ticker(sym).get("last")
            except Exception as e:
                logger.error("Kunde inte hämta pris för öppen position %s: %s", sym, e)
        if price:
            orphan_prices[sym] = float(price)

    if orphans:
        logger.info("Öppna positioner utanför bevakningslistan: %s",
                    ", ".join(sorted(orphans)))

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

    try:
        _run_bot_cycle(bots, candle_cache, fixed_symbols, orphan_prices, now,
                       flow_cache, regime_cache, hype_cache, kill_switch_on,
                       all_tickers)
    finally:
        # Skickas ALLTID, även om en bot kraschar mitt i cykeln.
        # Annars skulle affärer försvinna tyst ur notisflödet.
        _flush_notifications()


def _run_bot_cycle(bots, candle_cache, fixed_symbols, orphan_prices, now,
                   flow_cache, regime_cache, hype_cache, kill_switch_on,
                   all_tickers=None):
    all_tickers = all_tickers or {}
    for bot in bots:
        cls = strategies.STRATEGY_MAP.get(bot["strategy"])
        if cls is None:
            continue

        params = bot["params"] if isinstance(bot["params"], dict) else json.loads(bot["params"])
        positions = {p["symbol"]: p for p in _get_positions(bot["id"])}

        # --- Först: stäng positioner utanför bevakningslistan om reglerna säger det ---
        # Dessa kan inte utvärderas av strategin (ingen candle-data), men
        # säkerhetsnätet måste ändå gälla.
        for sym, pos in list(positions.items()):
            if sym in candle_cache or sym not in orphan_prices:
                continue
            price = orphan_prices[sym]
            entry = float(pos["entry_price"])
            pnl_pct = (price - entry) / entry * 100
            held_hours = (now - pos["opened_at"]).total_seconds() / 3600
            peak = max(float(pos["peak_price"] or 0), price)

            with get_cursor() as cur:
                cur.execute("UPDATE bot_positions SET peak_price = %s WHERE id = %s",
                            (peak, pos["id"]))

            exit_now, reason = False, ""
            if bot["strategy"] != "buy_and_hold":
                if pnl_pct <= HARD_STOP_LOSS_PCT:
                    exit_now, reason = True, f"SÄKERHETSSTOPP: {pnl_pct:.1f}%"
                elif held_hours >= MAX_HOLD_HOURS:
                    exit_now, reason = True, f"MAXTID: {held_hours:.0f}h ({pnl_pct:+.1f}%)"
                else:
                    exit_now, reason = momentum_strategy.check_exit(
                        {"avg_entry_price": entry, "peak_price": peak},
                        price, held_hours * 60,
                        take_profit_pct=ARENA_TAKE_PROFIT_PCT,
                        trailing_stop_pct=ARENA_TRAILING_STOP_PCT,
                        max_hold_minutes=ARENA_MAX_HOLD_MINUTES,
                        min_hold_minutes=ARENA_MIN_HOLD_MINUTES,
                    )
            if exit_now:
                fresh = next((b for b in list_bots() if b["id"] == bot["id"]), bot)
                _bot_sell(fresh, pos, price, reason + " [utanför bevakningslistan]")
                positions.pop(sym, None)

        for symbol, candles in candle_cache.items():
            # Referensboten ska ligga i de STORA paren, annars mäter den
            # inte "vad hade hänt om jag bara köpt och väntat" utan
            # "vad hade hänt om jag köpt slumpmässiga småtokens".
            if bot["strategy"] == "buy_and_hold" and symbol not in fixed_symbols:
                continue
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
                raw_signal = strat.signal(i)
                # Kräv att signalen håller i sig över flera cykler
                sig = _confirmed_signal(bot["id"], symbol, raw_signal)
                price = candles[i][4]

                pos = positions.get(symbol)

                if pos is None:
                    if sig == "buy":
                        # Kill switch: inga nya köp
                        if kill_switch_on:
                            continue
                        # Tak för antal samtidiga positioner
                        if len(positions) >= MAX_POSITIONS_PER_BOT:
                            continue
                        # Ladda om saldot — tidigare köp i samma cykel kan ha ändrat det
                        fresh = next((b for b in list_bots() if b["id"] == bot["id"]), bot)
                        slip = effective_slippage(symbol, all_tickers)
                        if _bot_buy(fresh, symbol, price,
                                    f"{strat.describe()} signal", slippage_pct=slip):
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

                    entry = float(pos["entry_price"])
                    pnl_pct = (price - entry) / entry * 100
                    held_hours = (now - pos["opened_at"]).total_seconds() / 3600

                    exit_now, reason = False, ""

                    # --- Säkerhetsnätet går FÖRE strategins egen logik ---
                    # Gäller alla bottar utom buy_and_hold, som per
                    # definition ska hålla oavsett vad som händer.
                    if bot["strategy"] != "buy_and_hold":
                        if pnl_pct <= HARD_STOP_LOSS_PCT:
                            exit_now = True
                            reason = f"SÄKERHETSSTOPP: {pnl_pct:.1f}% (gräns {HARD_STOP_LOSS_PCT}%)"
                        elif held_hours >= MAX_HOLD_HOURS:
                            exit_now = True
                            reason = f"MAXTID: {held_hours:.0f}h ({pnl_pct:+.1f}%)"

                    if not exit_now:
                        held_min = held_hours * 60
                        if cls.exit_mode == "signal":
                            # Minsta hålltid gäller även signalbaserade exits.
                            # Utan den stängdes positioner minuter efter köp.
                            if sig == "sell" and held_min >= ARENA_MIN_HOLD_MINUTES:
                                exit_now, reason = True, "SELL-signal"
                        else:
                            exit_now, reason = momentum_strategy.check_exit(
                                {"avg_entry_price": entry, "peak_price": peak},
                                price, held_min,
                                take_profit_pct=ARENA_TAKE_PROFIT_PCT,
                                trailing_stop_pct=ARENA_TRAILING_STOP_PCT,
                                max_hold_minutes=ARENA_MAX_HOLD_MINUTES,
                                min_hold_minutes=ARENA_MIN_HOLD_MINUTES,
                            )

                    if exit_now:
                        fresh = next((b for b in list_bots() if b["id"] == bot["id"]), bot)
                        _bot_sell(fresh, pos, price, reason,
                                  slippage_pct=effective_slippage(symbol, all_tickers))

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
