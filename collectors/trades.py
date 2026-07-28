"""Hämtar senaste avslutade trades för varje symbol."""
import logging
from collectors.exchange import EXCHANGE_NAME
from db import insert_trades

logger = logging.getLogger(__name__)


def collect_trades(exchange, symbols: list[str]):
    for symbol in symbols:
        try:
            trades = exchange.fetch_trades(symbol, limit=50)
            insert_trades(EXCHANGE_NAME, symbol, trades)
            logger.info("Trades %s: %d st sparade", symbol, len(trades))
        except Exception as e:
            logger.error("Trades-fel för %s: %s", symbol, e)
