"""
Skapar ccxt-instanser för KuCoin Spot och KuCoin Futures.
Publik marknadsdata (OHLCV, orderbok, trades, funding, OI) kräver INGA
API-nycklar — de behövs först i Execution Engine (Fas 2) när vi ska
lägga riktiga ordrar.
"""
import ccxt
from config import settings

EXCHANGE_NAME = "kucoin"


def make_spot_exchange() -> ccxt.Exchange:
    return ccxt.kucoin({
        "apiKey": settings.kucoin_api_key or None,
        "secret": settings.kucoin_api_secret or None,
        "password": settings.kucoin_api_passphrase or None,
        "enableRateLimit": True,
    })


def make_futures_exchange() -> ccxt.Exchange:
    return ccxt.kucoinfutures({
        "apiKey": settings.kucoin_api_key or None,
        "secret": settings.kucoin_api_secret or None,
        "password": settings.kucoin_api_passphrase or None,
        "enableRateLimit": True,
    })
