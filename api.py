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
from fastapi import FastAPI, Header, HTTPException, Query
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
