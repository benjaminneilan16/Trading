"""
Market Data Engine – huvudprogram.

Startar en separat tråd per datatyp (OHLCV, orderbok, trades, funding/OI),
var och en med sitt eget intervall från .env. Enkelt att förstå och
enkelt att bygga ut senare (t.ex. byta till asyncio eller en task-kö).

Kör med:
    python main.py
Avbryt med Ctrl+C.
"""
import logging
import threading
import time

from config import settings
from collectors.exchange import make_spot_exchange, make_futures_exchange
from collectors.ohlcv import collect_ohlcv
from collectors.orderbook import collect_orderbook
from collectors.trades import collect_trades
from collectors.funding import collect_funding_and_oi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

stop_event = threading.Event()


def loop(name: str, interval: int, func, *args):
    """Kör func(*args) om och om igen med `interval` sekunders mellanrum."""
    logger.info("Startar loop: %s (var %ds)", name, interval)
    while not stop_event.is_set():
        start = time.time()
        try:
            func(*args)
        except Exception:
            logger.exception("Oväntat fel i loop %s", name)
        elapsed = time.time() - start
        time.sleep(max(0.0, interval - elapsed))


def main():
    spot = make_spot_exchange()
    futures = make_futures_exchange()

    threads = [
        threading.Thread(
            target=loop,
            args=("ohlcv", settings.ohlcv_interval, collect_ohlcv, spot, settings.symbols),
            daemon=True,
        ),
        threading.Thread(
            target=loop,
            args=("orderbook", settings.orderbook_interval, collect_orderbook, spot, settings.symbols),
            daemon=True,
        ),
        threading.Thread(
            target=loop,
            args=("trades", settings.trades_interval, collect_trades, spot, settings.symbols),
            daemon=True,
        ),
        threading.Thread(
            target=loop,
            args=(
                "funding_oi",
                settings.funding_interval,
                collect_funding_and_oi,
                futures,
                settings.futures_symbols,
            ),
            daemon=True,
        ),
    ]

    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Avslutar...")
        stop_event.set()
        for t in threads:
            t.join(timeout=5)


if __name__ == "__main__":
    main()
