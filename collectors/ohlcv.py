"""Hämtar OHLCV-candles (1m som standard) för alla konfigurerade symboler."""
import logging
from collectors.exchange import EXCHANGE_NAME
from db import insert_ohlcv

logger = logging.getLogger(__name__)

TIMEFRAME = "1m"


def collect_ohlcv(exchange, symbols: list[str]):
    for symbol in symbols:
        try:
            candles = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=5)
            insert_ohlcv(EXCHANGE_NAME, symbol, TIMEFRAME, candles)
            logger.info("OHLCV %s: %d candles sparade", symbol, len(candles))
        except Exception as e:
            logger.error("OHLCV-fel för %s: %s", symbol, e)
