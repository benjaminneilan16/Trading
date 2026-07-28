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

logger = logging.getLogger("engine")


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

            jobs = [
                ("ohlcv", settings.ohlcv_interval, collect_ohlcv, spot, settings.symbols),
                ("orderbook", settings.orderbook_interval, collect_orderbook, spot, settings.symbols),
                ("trades", settings.trades_interval, collect_trades, spot, settings.symbols),
                ("funding_oi", settings.funding_interval, collect_funding_and_oi, futures, settings.futures_symbols),
            ]

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
