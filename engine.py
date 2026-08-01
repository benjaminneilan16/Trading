"""
EngineManager: samma insamlingslogik som tidigare i main.py, men
omgjord till en klass så den kan startas/stoppas via API-anrop
(t.ex. från din iPhone-dashboard) istället för bara Ctrl+C i terminalen.
"""
import logging
import threading
import time
from datetime import datetime, timezone

from config import settings
from collectors.exchange import make_spot_exchange, make_futures_exchange
from collectors.ohlcv import collect_ohlcv
from collectors.orderbook import collect_orderbook
from collectors.trades import collect_trades
from collectors.funding import collect_funding_and_oi
from db import get_ohlcv
import strategy
import paper_trading
import scanner
import momentum_strategy
import social
from notifier import send_notification

logger = logging.getLogger("engine")


def run_momentum_scan(exchange, max_positions: int, position_size_pct: float):
    """
    Letar efter tokens som börjar röra sig, och går in i de bästa.
    Körs mer sällan än exit-kollen (scanning är dyrt i API-anrop).
    """
    open_count = paper_trading.count_open_positions("momentum")
    slots_free = max_positions - open_count
    if slots_free <= 0:
        logger.info("Momentum: alla %d platser upptagna, skippar scan", max_positions)
        return

    hits = scanner.scan(exchange, min_score=momentum_strategy.MIN_ENTRY_SCORE)

    for hit in hits:
        if slots_free <= 0:
            break

        # Gå inte in i något vi redan äger
        if paper_trading.has_open_position(hit["symbol"]):
            paper_trading.log_scanner_hit(hit, entered=False)
            continue

        hype = social.hype_score(hit["symbol"])
        enter, reason = momentum_strategy.should_enter(hit, hype)

        paper_trading.log_scanner_hit(hit, entered=enter)

        if enter:
            paper_trading.buy_momentum(
                hit["symbol"], hit["last_price"], position_size_pct, reason=reason
            )
            send_notification(
                f"🟢 PAPER KÖP {hit['symbol']} @ {hit['last_price']:.8f}\n{reason}"
            )
            slots_free -= 1


def check_momentum_exits(exchange):
    """
    Kollar exit-reglerna för alla öppna momentum-positioner.
    Körs TÄTT (var 30:e sekund) — hela poängen med strategin är att
    komma ut snabbt när rörelsen vänder.
    """
    positions = paper_trading.get_open_positions("momentum")
    if not positions:
        return

    now = datetime.now(timezone.utc)

    for pos in positions:
        symbol = pos["symbol"]
        try:
            ticker = exchange.fetch_ticker(symbol)
            current_price = ticker["last"]
        except Exception as e:
            logger.error("Kunde inte hämta pris för %s: %s", symbol, e)
            continue

        if current_price is None:
            continue

        # Uppdatera topp-pris först, så trailing stop räknar rätt
        paper_trading.update_peak_price(symbol, current_price)
        if float(pos.get("peak_price") or 0) < current_price:
            pos["peak_price"] = current_price

        opened_at = pos["opened_at"]
        held_minutes = (now - opened_at).total_seconds() / 60

        should_exit, reason = momentum_strategy.check_exit(pos, current_price, held_minutes)

        if should_exit:
            result = paper_trading.sell(symbol, current_price, reason=reason)
            if result.get("executed"):
                pnl = result.get("realized_pnl", 0)
                emoji = "✅" if pnl >= 0 else "🔻"
                send_notification(
                    f"{emoji} PAPER SÄLJ {symbol} @ {current_price:.8f}\n"
                    f"{reason}\nResultat: {pnl:+.2f} USDT"
                )


def run_strategy_once(symbols: list[str]):
    """
    Körs på ett schema (STRATEGY_INTERVAL_SECONDS). För varje symbol:
    1. Hämta senaste candles
    2. Låt Decision Engine (strategy.py) fatta ett beslut
    3. Logga beslutet (även "avvakta", för transparens/felsökning)
    4. Om köp/sälj och tillståndet stämmer (t.ex. inte redan köpt) — exekvera i paper_trading
    """
    for symbol in symbols:
        candles = get_ohlcv(symbol, "1m", limit=100)
        if len(candles) < 26:
            continue  # inte tillräckligt med historik än

        result = strategy.decide(candles)
        paper_trading.log_signal(symbol, result)

        current_price = float(candles[-1]["close"])
        has_position = paper_trading.has_open_position(symbol)

        if result["decision"] == "buy" and not has_position:
            paper_trading.buy(symbol, current_price, reason=result["reason"])
        elif result["decision"] == "sell" and has_position:
            paper_trading.sell(symbol, current_price, reason=result["reason"])
        # "hold", eller buy/sell som inte matchar tillståndet -> gör inget


class EngineManager:
    def __init__(self):
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._running = False
        self._lock = threading.Lock()
        # Senaste lyckade körning per collector, för statusvisning i dashboarden
        self.last_run: dict[str, str] = {}
        self.errors: dict[str, str] = {}

    @property
    def running(self) -> bool:
        return self._running

    def _wrapped_loop(self, name: str, interval: int, func, *args):
        while not self._stop_event.is_set():
            start = time.time()
            try:
                func(*args)
                self.last_run[name] = datetime.now(timezone.utc).isoformat()
                self.errors.pop(name, None)
            except Exception as e:
                logger.exception("Fel i loop %s", name)
                self.errors[name] = str(e)
            elapsed = time.time() - start
            self._stop_event.wait(max(0.0, interval - elapsed))

    def start(self):
        with self._lock:
            if self._running:
                return {"status": "already_running"}

            self._stop_event.clear()
            spot = make_spot_exchange()
            futures = make_futures_exchange()
            paper_trading.ensure_wallet_exists(settings.paper_starting_balance)

            jobs = [
                ("ohlcv", settings.ohlcv_interval, collect_ohlcv, spot, settings.symbols),
                ("orderbook", settings.orderbook_interval, collect_orderbook, spot, settings.symbols),
                ("trades", settings.trades_interval, collect_trades, spot, settings.symbols),
                ("funding_oi", settings.funding_interval, collect_funding_and_oi, futures, settings.futures_symbols),
                ("strategy", settings.strategy_interval, run_strategy_once, settings.symbols),
            ]

            if settings.momentum_enabled:
                jobs.append((
                    "momentum_scan",
                    settings.scan_interval,
                    run_momentum_scan,
                    spot,
                    settings.momentum_max_positions,
                    settings.momentum_position_size_pct,
                ))
                jobs.append((
                    "momentum_exits",
                    momentum_strategy.EXIT_CHECK_INTERVAL,
                    check_momentum_exits,
                    spot,
                ))

            self._threads = [
                threading.Thread(target=self._wrapped_loop, args=job, daemon=True, name=job[0])
                for job in jobs
            ]
            for t in self._threads:
                t.start()

            self._running = True
            logger.info("Engine startad")
            return {"status": "started"}

    def stop(self):
        with self._lock:
            if not self._running:
                return {"status": "already_stopped"}
            self._stop_event.set()
            for t in self._threads:
                t.join(timeout=5)
            self._threads = []
            self._running = False
            logger.info("Engine stoppad")
            return {"status": "stopped"}

    def status(self) -> dict:
        return {
            "running": self._running,
            "symbols": settings.symbols,
            "futures_symbols": settings.futures_symbols,
            "last_run": self.last_run,
            "errors": self.errors,
        }


# En enda delad instans som api.py importerar
engine = EngineManager()
