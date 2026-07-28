"""
Hämtar funding rate och open interest från KuCoin Futures.
OBS: dessa finns bara för perpetual futures-kontrakt, inte för spot-par.
Symbolformat i ccxt för KuCoin Futures är t.ex. "BTC/USDT:USDT".
"""
import logging
import time
from collectors.exchange import EXCHANGE_NAME
from db import insert_funding_rate, insert_open_interest

logger = logging.getLogger(__name__)


def collect_funding_and_oi(futures_exchange, symbols: list[str]):
    now_ms = int(time.time() * 1000)
    for symbol in symbols:
        try:
            funding = futures_exchange.fetch_funding_rate(symbol)
            insert_funding_rate(
                EXCHANGE_NAME,
                symbol,
                funding.get("timestamp") or now_ms,
                funding.get("fundingRate"),
                funding.get("nextFundingTimestamp"),
            )
            logger.info("Funding %s: %s", symbol, funding.get("fundingRate"))
        except Exception as e:
            logger.error("Funding-fel för %s: %s", symbol, e)

        try:
            oi = futures_exchange.fetch_open_interest(symbol)
            insert_open_interest(
                EXCHANGE_NAME,
                symbol,
                oi.get("timestamp") or now_ms,
                oi.get("openInterestAmount") or oi.get("openInterestValue"),
                oi.get("openInterestValue"),
            )
            logger.info("Open interest %s sparad", symbol)
        except Exception as e:
            logger.error("Open interest-fel för %s: %s", symbol, e)
