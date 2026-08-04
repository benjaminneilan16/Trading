"""
API-server för Market Data Engine — komplett backend, byggd för att en
fristående frontend (t.ex. genererad i Lovable) ska kunna koppla in sig
mot den via REST.

Kör lokalt med:
    uvicorn api:app --host 0.0.0.0 --port 8000

Interaktiv API-dokumentation (Swagger UI) genereras automatiskt av
FastAPI på:
    http://<host>:8000/docs

Peka Lovable (eller vilken frontend som helst) mot den sidan, eller
mot /openapi.json för maskinläsbar spec — då slipper man beskriva
endpoints för hand.

AUTENTISERING
Alla endpoints kräver en header:  X-API-Key: <API_KEY från .env>
Det är den enda "inloggningen" — appen är byggd för en enda användare (dig).
"""
import logging
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Header, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from engine import engine
from notifier import send_notification
import db
import paper_trading
import social
import risk_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("api")

app = FastAPI(
    title="Crypto Market Data API",
    description="Backend för insamling och visning av kryptodata från KuCoin. "
                 "Byggd för att drivas av en fristående frontend (t.ex. Lovable).",
    version="1.0.0",
)

# Lovable-hostade frontends körs på ett annat domännamn än din backend,
# så CORS måste vara öppen för att webbläsaren ska tillåta anrop.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def check_key(x_api_key: Optional[str]):
    if x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Ogiltig eller saknad API-nyckel (X-API-Key header)")


# ---------------------------------------------------------------------------
# Svarsmodeller — ger Lovable/andra verktyg tydliga typer via /openapi.json
# ---------------------------------------------------------------------------

class StatusResponse(BaseModel):
    running: bool
    symbols: list[str]
    futures_symbols: list[str]
    last_run: dict
    errors: dict


class ActionResponse(BaseModel):
    status: str


class SettingsResponse(BaseModel):
    symbols: list[str]
    futures_symbols: list[str]
    ohlcv_interval: int
    orderbook_interval: int
    trades_interval: int
    funding_interval: int


class Candle(BaseModel):
    symbol: str
    timeframe: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class OrderbookSnapshot(BaseModel):
    symbol: str
    ts: datetime
    bids: list
    asks: list
    best_bid: Optional[float]
    best_ask: Optional[float]
    spread_pct: Optional[float]


class Trade(BaseModel):
    symbol: str
    trade_id: str
    ts: datetime
    side: str
    price: float
    amount: float


class FundingRate(BaseModel):
    symbol: str
    ts: datetime
    funding_rate: float
    next_funding: Optional[datetime]


class OpenInterest(BaseModel):
    symbol: str
    ts: datetime
    open_interest: float
    open_interest_usd: Optional[float]


class NotifyRequest(BaseModel):
    message: str


class PaperPosition(BaseModel):
    symbol: str
    amount: float
    avg_entry_price: float
    opened_at: datetime
    current_price: float
    value_usdt: float
    unrealized_pnl: float


class PaperPortfolio(BaseModel):
    quote_currency: str
    quote_balance: float
    starting_balance: float
    positions: list[PaperPosition]
    total_value: float
    total_pnl: float
    total_pnl_pct: float


class PaperTrade(BaseModel):
    symbol: str
    side: str
    price: float
    amount: float
    quote_amount: float
    realized_pnl: Optional[float]
    reason: Optional[str]
    ts: datetime


class StrategySignal(BaseModel):
    id: int
    symbol: str
    ts: datetime
    decision: str
    score: float
    ema_fast: Optional[float]
    ema_slow: Optional[float]
    rsi: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    reason: Optional[str]


# ---------------------------------------------------------------------------
# Motor-styrning
# ---------------------------------------------------------------------------

@app.get("/api/status", response_model=StatusResponse, tags=["engine"])
def get_status(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    return engine.status()


@app.post("/api/start", response_model=ActionResponse, tags=["engine"])
def start_engine(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    result = engine.start()
    send_notification("✅ Market Data Engine startad")
    return result


@app.post("/api/stop", response_model=ActionResponse, tags=["engine"])
def stop_engine(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    result = engine.stop()
    send_notification("🛑 Market Data Engine stoppad")
    return result


@app.get("/api/settings", response_model=SettingsResponse, tags=["engine"])
def get_settings(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    return {
        "symbols": settings.symbols,
        "futures_symbols": settings.futures_symbols,
        "ohlcv_interval": settings.ohlcv_interval,
        "orderbook_interval": settings.orderbook_interval,
        "trades_interval": settings.trades_interval,
        "funding_interval": settings.funding_interval,
    }


# ---------------------------------------------------------------------------
# Marknadsdata — det Lovable-frontenden hämtar för att rita grafer/tabeller
# ---------------------------------------------------------------------------

@app.get("/api/symbols", tags=["market-data"])
def list_symbols(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    return {"spot": settings.symbols, "futures": settings.futures_symbols}


@app.get("/api/ohlcv", response_model=list[Candle], tags=["market-data"])
def get_ohlcv(
    symbol: str = Query(..., description="T.ex. BTC/USDT"),
    timeframe: str = Query("1m", description="T.ex. 1m, 5m, 1h"),
    limit: int = Query(200, ge=1, le=1000),
    x_api_key: Optional[str] = Header(default=None),
):
    check_key(x_api_key)
    return db.get_ohlcv(symbol, timeframe, limit)


@app.get("/api/orderbook", response_model=Optional[OrderbookSnapshot], tags=["market-data"])
def get_orderbook(
    symbol: str = Query(..., description="T.ex. BTC/USDT"),
    x_api_key: Optional[str] = Header(default=None),
):
    check_key(x_api_key)
    snap = db.get_latest_orderbook(symbol)
    if snap is None:
        raise HTTPException(status_code=404, detail="Ingen orderboksdata ännu för den symbolen")
    return snap


@app.get("/api/trades", response_model=list[Trade], tags=["market-data"])
def get_trades(
    symbol: str = Query(..., description="T.ex. BTC/USDT"),
    limit: int = Query(50, ge=1, le=500),
    x_api_key: Optional[str] = Header(default=None),
):
    check_key(x_api_key)
    return db.get_recent_trades(symbol, limit)


@app.get("/api/funding", response_model=Optional[FundingRate], tags=["market-data"])
def get_funding(
    symbol: str = Query(..., description="T.ex. BTC/USDT:USDT (futures-format)"),
    x_api_key: Optional[str] = Header(default=None),
):
    check_key(x_api_key)
    rate = db.get_latest_funding_rate(symbol)
    if rate is None:
        raise HTTPException(status_code=404, detail="Ingen funding-data ännu för den symbolen")
    return rate


@app.get("/api/open-interest", response_model=Optional[OpenInterest], tags=["market-data"])
def get_open_interest(
    symbol: str = Query(..., description="T.ex. BTC/USDT:USDT (futures-format)"),
    x_api_key: Optional[str] = Header(default=None),
):
    check_key(x_api_key)
    oi = db.get_latest_open_interest(symbol)
    if oi is None:
        raise HTTPException(status_code=404, detail="Ingen open interest-data ännu för den symbolen")
    return oi


# ---------------------------------------------------------------------------
# Notiser
# ---------------------------------------------------------------------------

@app.post("/api/notify-test", tags=["notifications"])
def notify_test(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    ok = send_notification("🔔 Testnotis från din Market Data Engine-app")
    return {"sent": ok}


@app.post("/api/notify", tags=["notifications"])
def notify_custom(body: NotifyRequest, x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    ok = send_notification(body.message)
    return {"sent": ok}


# ---------------------------------------------------------------------------
# Paper trading — Decision Engine (regelbaserad) + simulerad exekvering
# ---------------------------------------------------------------------------

@app.get("/api/paper/portfolio", response_model=PaperPortfolio, tags=["paper-trading"])
def paper_portfolio(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    result = paper_trading.get_portfolio()
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/paper/trades", response_model=list[PaperTrade], tags=["paper-trading"])
def paper_trades(
    limit: int = Query(50, ge=1, le=500),
    x_api_key: Optional[str] = Header(default=None),
):
    check_key(x_api_key)
    return paper_trading.get_recent_trades(limit)


@app.get("/api/paper/signals", response_model=list[StrategySignal], tags=["paper-trading"])
def paper_signals(
    symbol: Optional[str] = Query(None, description="Filtrera på en symbol, t.ex. BTC/USDT"),
    limit: int = Query(20, ge=1, le=200),
    x_api_key: Optional[str] = Header(default=None),
):
    check_key(x_api_key)
    return paper_trading.get_recent_signals(symbol, limit)


@app.post("/api/paper/reset", tags=["paper-trading"])
def paper_reset(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    paper_trading.reset_wallet(settings.paper_starting_balance)
    send_notification("♻️ Papperskonto nollställt")
    return {"status": "reset", "starting_balance": settings.paper_starting_balance}


@app.get("/api/scanner/hits", tags=["momentum"])
def scanner_hits(
    limit: int = Query(50, ge=1, le=200),
    x_api_key: Optional[str] = Header(default=None),
):
    """Vad momentum-scannern hittat — inklusive kandidater den valde att INTE gå in i."""
    check_key(x_api_key)
    return paper_trading.get_scanner_hits(limit)


@app.get("/api/momentum/config", tags=["momentum"])
def momentum_config(x_api_key: Optional[str] = Header(default=None)):
    """Nuvarande inställningar för momentum-strategin — bra att visa i appen."""
    check_key(x_api_key)
    import momentum_strategy as ms
    import scanner as sc
    return {
        "enabled": settings.momentum_enabled,
        "scan_interval_seconds": settings.scan_interval,
        "exit_check_interval_seconds": ms.EXIT_CHECK_INTERVAL,
        "max_positions": settings.momentum_max_positions,
        "position_size_pct": settings.momentum_position_size_pct,
        "entry": {
            "min_score": ms.MIN_ENTRY_SCORE,
            "volume_spike_min": sc.VOLUME_SPIKE_MIN,
            "price_change_min_pct": sc.PRICE_CHANGE_MIN_PCT,
            "price_change_max_pct": sc.PRICE_CHANGE_MAX_PCT,
        },
        "exit": {
            "take_profit_pct": ms.TAKE_PROFIT_PCT,
            "stop_loss_pct": ms.STOP_LOSS_PCT,
            "trailing_stop_pct": ms.TRAILING_STOP_PCT,
            "max_hold_minutes": ms.MAX_HOLD_MINUTES,
        },
        "liquidity_filter": {
            "min_24h_volume_usdt": sc.MIN_24H_QUOTE_VOLUME,
            "max_24h_volume_usdt": sc.MAX_24H_QUOTE_VOLUME,
            "max_spread_pct": sc.MAX_SPREAD_PCT,
        },
        "social_hype": social.hype_score("BTC/USDT"),
    }


@app.on_event("startup")
def on_startup():
    if settings.auto_start:
        logger.info("AUTO_START är på — startar insamlingen automatiskt")
        engine.start()


@app.get("/", tags=["meta"])
def root():
    return {
        "name": "Crypto Market Data API",
        "docs": "/docs",
        "openapi_spec": "/openapi.json",
    }


# ---------------------------------------------------------------------------
# Risk Manager (Fas 3)
# ---------------------------------------------------------------------------

class KillSwitchRequest(BaseModel):
    reason: str = "Manuellt stoppad från appen"


@app.get("/api/risk/status", tags=["risk"])
def risk_status(x_api_key: Optional[str] = Header(default=None)):
    """
    Hela riskläget: kill switch, dagens resultat, exponering, och hur
    mycket utrymme som finns kvar innan varje gräns slår i taket.
    """
    check_key(x_api_key)
    portfolio = paper_trading.get_portfolio()
    if "error" in portfolio:
        raise HTTPException(status_code=404, detail=portfolio["error"])
    return risk_manager.risk_summary(portfolio)


@app.get("/api/risk/daily", tags=["risk"])
def risk_daily(x_api_key: Optional[str] = Header(default=None)):
    """Dagens statistik: resultat, antal affärer, vinstandel."""
    check_key(x_api_key)
    return risk_manager.get_daily_stats()


@app.post("/api/risk/kill-switch/activate", tags=["risk"])
def kill_switch_on(
    body: KillSwitchRequest,
    x_api_key: Optional[str] = Header(default=None),
):
    """NÖDSTOPP: stoppar all ny handel omedelbart. Öppna positioner ligger kvar."""
    check_key(x_api_key)
    risk_manager.activate_kill_switch(body.reason)
    send_notification(f"🛑 KILL SWITCH AKTIVERAD\n{body.reason}")
    return {"status": "activated", "reason": body.reason}


@app.post("/api/risk/kill-switch/deactivate", tags=["risk"])
def kill_switch_off(x_api_key: Optional[str] = Header(default=None)):
    """Släpper på handeln igen efter ett stopp."""
    check_key(x_api_key)
    risk_manager.deactivate_kill_switch()
    send_notification("▶️ Kill switch avaktiverad — handel tillåten igen")
    return {"status": "deactivated"}


@app.post("/api/risk/close-all", tags=["risk"])
def close_all_positions(x_api_key: Optional[str] = Header(default=None)):
    """
    Stänger ALLA öppna positioner till senaste kända pris, och aktiverar
    kill switch. Det här är den stora röda knappen.
    """
    check_key(x_api_key)
    positions = paper_trading.get_open_positions()
    closed = []

    for pos in positions:
        symbol = pos["symbol"]
        latest = db.get_ohlcv(symbol, "1m", limit=1)
        price = float(latest[-1]["close"]) if latest else float(pos["avg_entry_price"])
        result = paper_trading.sell(symbol, price, reason="MANUELL STÄNGNING (close-all)")
        if result.get("executed"):
            closed.append({"symbol": symbol, "realized_pnl": result.get("realized_pnl")})

    risk_manager.activate_kill_switch("Alla positioner stängda manuellt")
    total_pnl = sum(c["realized_pnl"] or 0 for c in closed)
    send_notification(f"🚨 ALLA POSITIONER STÄNGDA\n{len(closed)} st, resultat {total_pnl:+.2f} USDT")
    return {"closed": closed, "total_realized_pnl": total_pnl}


# ---------------------------------------------------------------------------
# Backtesting (Fas 5)
# ---------------------------------------------------------------------------

@app.get("/api/backtest/run", tags=["backtest"])
def run_backtest(
    symbol: str = Query(..., description="T.ex. BTC/USDT"),
    strategy_name: str = Query("momentum", description="'momentum' eller 'technical'"),
    timeframe: str = Query("5m", description="1m, 5m, 15m, 1h"),
    limit: int = Query(1000, ge=100, le=1500, description="Antal candles bakåt"),
    starting_balance: float = Query(1000.0, gt=0),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Kör strategin mot historisk data och returnerar nyckeltal.

    Tar 5-20 sekunder beroende på antal candles. Avgifter (0,1%/sida) och
    slippage (0,15%/sida) är inräknade — resultatet är alltså vad du
    ungefär hade fått, inte ett teoretiskt bästa fall.
    """
    check_key(x_api_key)
    import backtest
    from collectors.exchange import make_spot_exchange

    exchange = make_spot_exchange()
    result = backtest.run(
        exchange, symbol, strategy_name, timeframe, limit, starting_balance
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/backtest/compare", tags=["backtest"])
def compare_backtest(
    symbols: str = Query(..., description="Kommaseparerat, t.ex. BTC/USDT,ETH/USDT,SOL/USDT"),
    strategy_name: str = Query("momentum"),
    timeframe: str = Query("5m"),
    limit: int = Query(500, ge=100, le=1000),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Kör samma strategi på flera symboler och jämför.

    Detta är den viktigaste vyn: en strategi som bara fungerar på EN token
    är oftast överanpassad (tur), inte en fungerande strategi.
    """
    check_key(x_api_key)
    import backtest
    from collectors.exchange import make_spot_exchange

    exchange = make_spot_exchange()
    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()][:10]

    results = []
    for sym in symbol_list:
        try:
            r = backtest.run(exchange, sym, strategy_name, timeframe, limit)
            if "error" not in r:
                results.append(r["metrics"])
        except Exception as e:
            logger.error("Backtest misslyckades för %s: %s", sym, e)

    if not results:
        raise HTTPException(status_code=400, detail="Ingen backtest kunde köras")

    profitable = [r for r in results if r["total_return_pct"] > 0]
    beat_hold = [r for r in results if r.get("beat_buy_and_hold")]

    return {
        "results": results,
        "summary": {
            "symbols_tested": len(results),
            "profitable": len(profitable),
            "beat_buy_and_hold": len(beat_hold),
            "avg_return_pct": round(sum(r["total_return_pct"] for r in results) / len(results), 2),
            "total_trades": sum(r["trades"] for r in results),
            "verdict": _backtest_verdict(results),
        },
    }


def _backtest_verdict(results: list[dict]) -> str:
    """En ärlig sammanfattning istället för bara siffror."""
    total_trades = sum(r["trades"] for r in results)
    if total_trades < 20:
        return ("För få affärer för att dra slutsatser. Kör längre period "
                "eller fler symboler — under ~30 affärer är resultatet mest slump.")

    profitable = len([r for r in results if r["total_return_pct"] > 0])
    ratio = profitable / len(results)

    if ratio >= 0.7:
        return ("Lovande: lönsam på de flesta symboler. Det tyder på att det "
                "inte bara är tur på en enskild token. Kör vidare på papper.")
    if ratio >= 0.4:
        return ("Blandat: lönsam på ungefär hälften. Kan vara marknadsberoende "
                "snarare än en fungerande edge. Testa i en annan tidsperiod.")
    return ("Svagt: förlust på de flesta symboler. Justera trösklarna, eller "
            "acceptera att strategin inte har någon edge i detta marknadsläge.")


# ---------------------------------------------------------------------------
# Strategy Lab — masstestning med skydd mot överanpassning
# ---------------------------------------------------------------------------

# Labbet tar flera minuter, så det körs i bakgrunden och resultatet
# hämtas separat. Annars skulle HTTP-anropet timea ut.
_lab_state = {"status": "idle", "started_at": None, "finished_at": None,
              "result": None, "error": None}


def _run_lab_job(symbols: list[str], timeframe: str, limit: int):
    import lab
    from collectors.exchange import make_spot_exchange
    from datetime import datetime, timezone

    _lab_state.update({
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": None, "result": None, "error": None,
    })
    try:
        exchange = make_spot_exchange()
        result = lab.run_lab(exchange, symbols, timeframe, limit)
        _lab_state.update({
            "status": "done",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        })
        verdict = result.get("verdict", {}).get("summary", "")
        send_notification(f"🧪 Strategitest klart\n{verdict}")
    except Exception as e:
        logger.exception("Lab-körning misslyckades")
        _lab_state.update({
            "status": "error",
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": str(e),
        })


@app.post("/api/lab/start", tags=["lab"])
def lab_start(
    background_tasks: BackgroundTasks,
    symbols: str = Query("BTC/USDT,ETH/USDT,SOL/USDT", description="Kommaseparerat"),
    timeframe: str = Query("5m"),
    limit: int = Query(1000, ge=300, le=1500),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Startar en full genomsökning: alla strategier × alla parametrar ×
    alla symboler, med tränings/testuppdelning.

    Körs i bakgrunden (tar 1-5 minuter). Följ förloppet via /api/lab/status.
    """
    check_key(x_api_key)
    if _lab_state["status"] == "running":
        raise HTTPException(status_code=409, detail="En körning pågår redan")

    symbol_list = [s.strip() for s in symbols.split(",") if s.strip()][:10]
    background_tasks.add_task(_run_lab_job, symbol_list, timeframe, limit)
    return {"status": "started", "symbols": symbol_list, "timeframe": timeframe}


@app.get("/api/lab/status", tags=["lab"])
def lab_status(x_api_key: Optional[str] = Header(default=None)):
    """Status och (när klart) hela resultatet från senaste labbkörningen."""
    check_key(x_api_key)
    return _lab_state


@app.get("/api/lab/strategies", tags=["lab"])
def lab_strategies(x_api_key: Optional[str] = Header(default=None)):
    """Vilka strategier som finns i biblioteket och hur många kombinationer de ger."""
    check_key(x_api_key)
    import strategies as st
    out = []
    total = 0
    for cls in st.ALL_STRATEGIES:
        combos = st.expand_grid(cls)
        total += len(combos)
        out.append({
            "name": cls.name,
            "exit_mode": cls.exit_mode,
            "param_grid": cls.param_grid,
            "combinations": len(combos),
        })
    return {"strategies": out, "total_combinations": total}


# ---------------------------------------------------------------------------
# Social / hype
# ---------------------------------------------------------------------------

@app.get("/api/social/hype", tags=["social"])
def social_hype(
    symbol: str = Query(..., description="T.ex. PEPE/USDT"),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Hype-score från Reddit. Mäter FÖRÄNDRING i omnämnanden, inte antal —
    annars skulle BTC alltid vinna.
    """
    check_key(x_api_key)
    return social.hype_score(symbol)


# ---------------------------------------------------------------------------
# Bot-arena — en bot per strategi, alla tävlar samtidigt
# ---------------------------------------------------------------------------

class CreateBotRequest(BaseModel):
    name: str
    strategy: str
    params: dict = {}
    starting_balance: float = 1000.0
    position_size_pct: float = 0.20


@app.get("/api/bots/leaderboard", tags=["bots"])
def bots_leaderboard(x_api_key: Optional[str] = Header(default=None)):
    """
    Topplistan: alla bottar rankade på avkastning, med ärlig kontext om
    hur mycket siffrorna faktiskt är värda än.

    Läs 'summary.verdict' FÖRST, innan du tittar på placeringarna.
    """
    check_key(x_api_key)
    import bots
    return bots.leaderboard()


@app.get("/api/bots", tags=["bots"])
def bots_list(x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    import bots
    return bots.list_bots()


@app.get("/api/bots/analysis", tags=["bots"])
def bots_analysis(x_api_key: Optional[str] = Header(default=None)):
    """
    Djupanalys av alla bottar — skiljer strategifel från friktionsfel.

    Det avgörande måttet är BRUTTO mot NETTO: hur mycket priset rörde sig
    mellan köp och sälj, jämfört med vad boten fick behålla.

      Brutto positivt, netto negativt -> strategin har kant men handlar
                                          för ofta. Justerbart.
      Brutto negativt                 -> strategin gissar fel. Dött spår.

    I topplistan ser båda fallen likadana ut. Här syns skillnaden.
    """
    check_key(x_api_key)
    import analysis
    return analysis.analyze_all()


@app.get("/api/bots/diagnostics", tags=["bots"])
def bot_diagnostics(x_api_key: Optional[str] = Header(default=None)):
    """
    Vad tycker varje bot om varje symbol JUST NU?

    Finns för att svara på frågan "varför händer ingenting?". Utan detta
    är en bot med noll affärer omöjlig att skilja från en trasig bot —
    båda ser likadana ut i topplistan.
    """
    check_key(x_api_key)
    import strategies as st
    import bots as bot_mod
    import orderflow, regime as regime_mod, social
    from db import get_ohlcv
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # Hämta candles en gång per symbol
    candle_cache = {}
    for sym in settings.symbols:
        rows = get_ohlcv(sym, "1m", limit=200)
        if len(rows) < 60:
            continue
        age_min = (now - rows[-1]["ts"]).total_seconds() / 60
        if age_min > bot_mod.MAX_CANDLE_AGE_MINUTES:
            continue
        candle_cache[sym] = [
            [int(r["ts"].timestamp() * 1000), float(r["open"]), float(r["high"]),
             float(r["low"]), float(r["close"]), float(r["volume"])]
            for r in rows
        ]

    if not candle_cache:
        return {"error": "Ingen färsk candle-data tillgänglig", "symbols_checked": settings.symbols}

    # Kontext för de strategier som behöver den
    flow_cache, regime_cache, hype_cache = {}, {}, {}
    for sym, candles in candle_cache.items():
        try:
            flow_cache[sym] = orderflow.get_flow_metrics(sym, 15)
        except Exception:
            pass
        try:
            regime_cache[sym] = regime_mod.detect_regime(candles)
        except Exception:
            pass
        try:
            hype_cache[sym] = social.hype_score(sym)
        except Exception:
            pass

    results = []
    for bot in bot_mod.list_bots():
        cls = st.STRATEGY_MAP.get(bot["strategy"])
        if cls is None:
            continue
        params = bot["params"] if isinstance(bot["params"], dict) else {}
        held = {p["symbol"] for p in bot_mod._get_positions(bot["id"])}

        signals = {}
        for sym, candles in candle_cache.items():
            try:
                strat = cls(**params)
                if cls.needs_context:
                    if cls.name == "ensemble_ai":
                        strat.context = {
                            "symbol": sym, "flow": flow_cache.get(sym),
                            "regime": regime_cache.get(sym), "hype": hype_cache.get(sym),
                        }
                    else:
                        strat.context = flow_cache.get(sym)
                strat.prepare(candles)
                signals[sym] = strat.signal(len(candles) - 1)
            except Exception as e:
                signals[sym] = f"fel: {e}"

        buys = [s for s, v in signals.items() if v == "buy"]
        results.append({
            "bot": bot["name"],
            "strategy": bot["strategy"],
            "enabled": bot["enabled"],
            "open_positions": sorted(held),
            "signals": signals,
            "buy_signals_now": buys,
            "would_trade": bool([s for s in buys if s not in held]),
        })

    active = [r for r in results if r["buy_signals_now"]]

    return {
        "checked_at": now,
        "symbols_with_fresh_data": sorted(candle_cache.keys()),
        "bots": results,
        "summary": {
            "bots_with_buy_signal": len(active),
            "bots_total": len(results),
            "note": (
                "Ingen bot har köpsignal just nu. Det är normalt — de flesta "
                "strategier väntar på specifika villkor som inträffar sällan. "
                "Är det så här i flera dygn, kolla att trösklarna inte är för strikta."
            ) if not active else
            f"{len(active)} bottar har köpsignal just nu.",
        },
    }


# ---------------------------------------------------------------------------
# Databasunderhåll
# ---------------------------------------------------------------------------


@app.get("/api/bots/{bot_id}", tags=["bots"])
def bot_detail(bot_id: int, x_api_key: Optional[str] = Header(default=None)):
    """Full statistik för en enskild bot, inklusive öppna positioner."""
    check_key(x_api_key)
    import bots
    result = bots.bot_stats(bot_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/bots/{bot_id}/trades", tags=["bots"])
def bot_trades(
    bot_id: int,
    limit: int = Query(50, ge=1, le=500),
    x_api_key: Optional[str] = Header(default=None),
):
    check_key(x_api_key)
    from db import get_cursor
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT symbol, side, price, amount, quote_amount, realized_pnl, "
            "fees_paid, reason, ts FROM bot_trades WHERE bot_id = %s "
            "ORDER BY ts DESC LIMIT %s",
            (bot_id, limit),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


@app.post("/api/bots/seed", tags=["bots"])
def bots_seed(x_api_key: Optional[str] = Header(default=None)):
    """
    Skapar en bot per strategi i biblioteket, alla med samma startkapital.
    Befintliga bottar rörs inte. Kör en gång för att starta arenan.
    """
    check_key(x_api_key)
    import bots
    created = bots.seed_default_bots(settings.bots_starting_balance)
    send_notification(f"🤖 Bot-arena startad: {len(created)} bottar skapade")
    return {"created": created, "count": len(created)}


@app.post("/api/bots/create", tags=["bots"])
def bots_create(body: CreateBotRequest, x_api_key: Optional[str] = Header(default=None)):
    """Skapa en egen bot med valfria parametrar."""
    check_key(x_api_key)
    import bots
    result = bots.create_bot(
        body.name, body.strategy, body.params,
        body.starting_balance, body.position_size_pct,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/bots/reset-all", tags=["bots"])
def bots_reset_all(x_api_key: Optional[str] = Header(default=None)):
    """
    Nollställer ALLA bottar. Använd när du vill starta en ny mätperiod,
    t.ex. efter att ha ändrat strategiparametrar.
    """
    check_key(x_api_key)
    import bots
    bots.reset_all_bots()
    send_notification("♻️ Alla bottar nollställda — ny mätperiod startad")
    return {"status": "all_reset"}


# ---------------------------------------------------------------------------
# Order flow — vad köpare och säljare faktiskt gör
# ---------------------------------------------------------------------------


@app.post("/api/bots/{bot_id}/enable", tags=["bots"])
def bot_enable(bot_id: int, enabled: bool = Query(True),
               x_api_key: Optional[str] = Header(default=None)):
    check_key(x_api_key)
    import bots
    bots.set_enabled(bot_id, enabled)
    return {"id": bot_id, "enabled": enabled}


@app.post("/api/bots/{bot_id}/reset", tags=["bots"])
def bot_reset(bot_id: int, x_api_key: Optional[str] = Header(default=None)):
    """Nollställer EN bot: saldo tillbaka till start, historik raderas."""
    check_key(x_api_key)
    import bots
    bots.reset_bot(bot_id)
    return {"id": bot_id, "status": "reset"}


@app.get("/api/orderflow", tags=["orderflow"])
def get_orderflow(
    symbol: str = Query(..., description="T.ex. BTC/USDT"),
    window_minutes: int = Query(15, ge=1, le=120),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Order flow-analys från din egen insamlade data.

    Läser `trades` och `orderbook_snapshots` och räknar ut:
    CVD (aggressiva köp minus säljningar), orderboksobalans, valprintar
    (affärer 8x medianen), absorption (hög volym men priset står still),
    och en sammanvägd score mellan -1 och +1.
    """
    check_key(x_api_key)
    import orderflow
    return orderflow.get_flow_metrics(symbol, window_minutes)


@app.get("/api/orderflow/all", tags=["orderflow"])
def get_orderflow_all(
    window_minutes: int = Query(15, ge=1, le=120),
    x_api_key: Optional[str] = Header(default=None),
):
    """Order flow för alla bevakade symboler, sorterat på score."""
    check_key(x_api_key)
    import orderflow
    data = orderflow.get_flow_for_symbols(settings.symbols, window_minutes)
    available = [v for v in data.values() if v.get("available")]
    available.sort(key=lambda x: x["score"], reverse=True)
    return {
        "symbols": data,
        "ranked": available,
        "strongest_buy_pressure": available[0]["symbol"] if available else None,
    }


# ---------------------------------------------------------------------------
# Regim, korrelation, equity, beslutsmotor
# ---------------------------------------------------------------------------

@app.get("/api/regime", tags=["analysis"])
def get_regime(x_api_key: Optional[str] = Header(default=None)):
    """
    Vilken sorts marknad är det just nu — trendande eller sidledes?

    Detta förklarar VARFÖR vissa bottar leder. Trendföljare tjänar pengar
    när priset rör sig rakt; mean reversion när det svänger sidledes.
    Fältet 'favors' visar vilka strategier som brukar passa regimen.
    """
    check_key(x_api_key)
    import regime
    return regime.regime_for_symbols(settings.symbols)


@app.get("/api/correlation", tags=["analysis"])
def get_correlation(x_api_key: Optional[str] = Header(default=None)):
    """
    Korrelation mellan bevakade symboler.

    Par över 0,75 rör sig i praktiken likadant — att hålla flera av dem
    samtidigt är inte riskspridning utan en större position i samma sak.
    """
    check_key(x_api_key)
    import correlation
    return correlation.correlation_matrix(settings.symbols)


@app.get("/api/decision", tags=["analysis"])
def get_decision(
    symbol: str = Query(..., description="T.ex. BTC/USDT"),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    AI Decision Engine (Fas 8): väger samman teknisk analys, order flow,
    social hype och marknadsregim till ett beslut — med full uppdelning
    av hur varje källa bidrog.
    """
    check_key(x_api_key)
    import decision_engine, orderflow, regime, social
    from db import get_ohlcv

    rows = get_ohlcv(symbol, "5m", limit=150)
    if len(rows) < 60:
        raise HTTPException(status_code=400, detail="För lite candle-data för denna symbol")

    candles = [
        [int(r["ts"].timestamp() * 1000), float(r["open"]), float(r["high"]),
         float(r["low"]), float(r["close"]), float(r["volume"])]
        for r in rows
    ]

    return decision_engine.decide(
        symbol,
        candles,
        flow=orderflow.get_flow_metrics(symbol),
        hype=social.hype_score(symbol),
        regime_data=regime.detect_regime(candles),
    )


@app.get("/api/bots/{bot_id}/equity", tags=["bots"])
def bot_equity(
    bot_id: int,
    hours: int = Query(168, ge=1, le=8760),
    x_api_key: Optional[str] = Header(default=None),
):
    """Kapitalkurva för en bot — riktiga värden, inklusive orealiserade positioner."""
    check_key(x_api_key)
    import reporting
    return {
        "curve": reporting.get_equity_curve(bot_id, hours),
        "drawdown": reporting.true_drawdown(bot_id),
    }


@app.get("/api/report/daily", tags=["analysis"])
def daily_report(x_api_key: Optional[str] = Header(default=None)):
    """Dygnsrapporten som text — samma som skickas till Telegram varje morgon."""
    check_key(x_api_key)
    import reporting
    return {"report": reporting.build_daily_report()}


@app.post("/api/report/send", tags=["analysis"])
def send_report_now(x_api_key: Optional[str] = Header(default=None)):
    """Skicka dygnsrapporten till Telegram direkt, utan att vänta på schemat."""
    check_key(x_api_key)
    import reporting
    report = reporting.build_daily_report()
    ok = send_notification(report)
    return {"sent": ok, "report": report}


# ---------------------------------------------------------------------------
# Nya listningar
# ---------------------------------------------------------------------------

@app.get("/api/listings/new", tags=["listings"])
def new_listings(
    max_age_days: int = Query(30, ge=1, le=180),
    x_api_key: Optional[str] = Header(default=None),
):
    """
    Tokens under angiven ålder, sorterat på ålder (yngst först).

    Åldern mäts genom att räkna dagliga candles — KuCoin säger inte när
    ett par listades, men candles finns bara från listningsdagen.
    """
    check_key(x_api_key)
    import newlistings
    tokens = newlistings.get_new_tokens(max_age_days)
    return {
        "tokens": tokens,
        "count": len(tokens),
        "warning": (
            "Nya listningar är den mest riskfyllda kategorin. Listningspumpar "
            "vänder ofta hårt, spreadarna är bredare och likviditeten tunnare. "
            "Det är också här pump-and-dump är vanligast."
        ),
    }


@app.get("/api/listings/stats", tags=["listings"])
def listing_stats(x_api_key: Optional[str] = Header(default=None)):
    """Status för symbolregistret: hur många som är kända och åldersbestämda."""
    check_key(x_api_key)
    import newlistings
    return newlistings.registry_stats()


@app.post("/api/listings/sync", tags=["listings"])
def sync_listings(x_api_key: Optional[str] = Header(default=None)):
    """Synka symbolregistret direkt istället för att vänta på schemat."""
    check_key(x_api_key)
    import newlistings
    from collectors.exchange import make_spot_exchange
    return newlistings.sync_registry(make_spot_exchange())


@app.post("/api/listings/check-ages", tags=["listings"])
def check_ages(x_api_key: Optional[str] = Header(default=None)):
    """
    Åldersbestäm nästa omgång okontrollerade symboler.

    Kör detta några gånger efter första uppsättningen — varje anrop
    kontrollerar 15 symboler, och registret innehåller hundratals.
    """
    check_key(x_api_key)
    import newlistings
    from collectors.exchange import make_spot_exchange
    checked = newlistings.check_pending_ages(make_spot_exchange())
    stats = newlistings.registry_stats()
    return {"checked_now": checked, **stats}


@app.get("/api/health", tags=["meta"])
def health_check(x_api_key: Optional[str] = Header(default=None)):
    """
    Är systemet friskt? Kollar att datainsamlingen faktiskt levererar
    färsk data — utan detta kan bottarna handla på frusna priser i
    timmar utan att någon märker det.
    """
    check_key(x_api_key)
    from datetime import datetime, timezone
    from db import get_cursor

    now = datetime.now(timezone.utc)
    checks = []

    with get_cursor(commit=False) as cur:
        for sym in settings.symbols:
            cur.execute(
                "SELECT MAX(ts) FROM ohlcv WHERE symbol = %s AND timeframe = '1m'",
                (sym,),
            )
            row = cur.fetchone()
            latest = row[0] if row else None
            age_min = (now - latest).total_seconds() / 60 if latest else None
            checks.append({
                "symbol": sym,
                "latest_candle": latest,
                "age_minutes": round(age_min, 1) if age_min is not None else None,
                "fresh": age_min is not None and age_min <= 15,
            })

        cur.execute("SELECT MAX(ts) FROM trades")
        latest_trade = cur.fetchone()[0]
        cur.execute("SELECT MAX(ts) FROM orderbook_snapshots")
        latest_book = cur.fetchone()[0]

    stale = [c["symbol"] for c in checks if not c["fresh"]]

    return {
        "engine_running": engine.running,
        "ohlcv": checks,
        "latest_trade": latest_trade,
        "latest_orderbook": latest_book,
        "stale_symbols": stale,
        "healthy": engine.running and not stale,
        "note": (
            f"{len(stale)} symboler har inaktuell data — bottarna hoppar över dem "
            "tills insamlingen kommit ikapp."
        ) if stale else "All data är färsk.",
    }


@app.get("/api/db/size", tags=["meta"])
def db_size(x_api_key: Optional[str] = Header(default=None)):
    """
    Storlek per tabell. Kolla denna om Railway varnar för full disk —
    den visar direkt vad som växer.
    """
    check_key(x_api_key)
    import cleanup
    return cleanup.database_size()


@app.post("/api/db/cleanup", tags=["meta"])
def db_cleanup(x_api_key: Optional[str] = Header(default=None)):
    """Kör städningen direkt istället för att vänta på schemat."""
    check_key(x_api_key)
    import cleanup
    return {"deleted": cleanup.run_cleanup(), **cleanup.database_size()}


@app.post("/api/db/emergency-truncate", tags=["meta"])
def db_emergency(x_api_key: Optional[str] = Header(default=None)):
    """
    NÖDLÄGE: tömmer orderbook_snapshots helt och frigör utrymmet direkt.

    Använd när databasen är nästan full. Datan är säker att kasta —
    order flow använder bara senaste snapshotten.
    """
    check_key(x_api_key)
    import cleanup
    result = cleanup.emergency_truncate_orderbook()
    send_notification(f"🧹 Nödtömning: {result['freed_mb']} MB frigjort")
    return result


@app.get("/api/bots/{bot_id}/analysis", tags=["bots"])
def bot_analysis_single(bot_id: int, x_api_key: Optional[str] = Header(default=None)):
    """Samma analys för en enskild bot."""
    check_key(x_api_key)
    import analysis
    from db import get_cursor
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, name, strategy FROM bots WHERE id = %s", (bot_id,))
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Bot finns inte")
    return analysis.analyze_bot(row[0], row[1], row[2])
