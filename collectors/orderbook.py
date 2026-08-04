"""Hämtar en orderbok-snapshot (topp N nivåer) för varje symbol."""
import logging
from collectors.exchange import EXCHANGE_NAME
from db import insert_orderbook_snapshot

logger = logging.getLogger(__name__)

# Antal nivåer per sida vi SPARAR i databasen.
#
# Sänkt från 20 till 8: orderboksdata var det som fyllde databasen till
# 95%. Order flow-analysen summerar bara djupet för att räkna obalans,
# och de översta nivåerna står för det mesta av den informationen —
# nivå 15-20 påverkar knappt resultatet men kostar lika mycket plats.
DEPTH = 8


def collect_orderbook(exchange, symbols: list[str]):
    for symbol in symbols:
        try:
            ob = exchange.fetch_order_book(symbol, limit=DEPTH)
            bids = ob["bids"][:DEPTH]
            asks = ob["asks"][:DEPTH]
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
