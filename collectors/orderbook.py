"""Hämtar en orderbok-snapshot (topp N nivåer) för varje symbol."""
import logging
from collectors.exchange import EXCHANGE_NAME
from db import insert_orderbook_snapshot

logger = logging.getLogger(__name__)

# KuCoin godtar BARA 20 eller 100 som djup i fetchOrderBook().
# Ett försök att sätta 8 gav "limit argument must be 20 or 100" och
# orderboksinsamlingen slutade fungera helt.
FETCH_DEPTH = 20

# Men vi behöver inte SPARA alla 20 nivåer. Order flow summerar bara
# djupet för att räkna obalans, och de översta nivåerna står för det
# mesta av informationen. Att spara färre nivåer var det som höll
# databasen från att fyllas.
STORE_DEPTH = 8


def collect_orderbook(exchange, symbols: list[str]):
    for symbol in symbols:
        try:
            ob = exchange.fetch_order_book(symbol, limit=FETCH_DEPTH)
            # Hämta 20 (KuCoins krav), spara 8 (diskutrymme)
            bids = ob["bids"][:STORE_DEPTH]
            asks = ob["asks"][:STORE_DEPTH]
            best_bid = bids[0][0] if bids else None
            best_ask = asks[0][0] if asks else None
            spread_pct = (
                (best_ask - best_bid) / best_bid * 100
                if best_bid and best_ask
                else None
            )
            insert_orderbook_snapshot(
                EXCHANGE_NAME, symbol, ob["timestamp"], bids, asks, best_bid, best_ask, spread_pct
            )
            logger.info("Orderbok %s: spread %.4f%%", symbol, spread_pct or 0)
        except Exception as e:
            logger.error("Orderboks-fel för %s: %s", symbol, e)
