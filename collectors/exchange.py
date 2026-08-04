"""
Skapar ccxt-instanser för KuCoin Spot och KuCoin Futures.
Publik marknadsdata (OHLCV, orderbok, trades, funding, OI) kräver INGA
API-nycklar — de behövs först i Execution Engine (Fas 2) när vi ska
lägga riktiga ordrar.
"""
import logging
import ccxt
from config import settings

logger = logging.getLogger("exchange")

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


def fetch_all_tickers(exchange) -> dict:
    """
    Hämtar ticker för alla marknader — utan att gå via ccxt:s fetch_tickers().

    VARFÖR INTE ccxt: KuCoin har par som existerar på flera marknader med
    samma id (t.ex. CLANKER-USDT på både spot och margin). ccxt vägrar då
    tolka svaret och kastar:

        safeMarket() requires a fourth argument for CLANKER-USDT to
        disambiguate between different markets with the same market id

    Det gör att HELA anropet misslyckas — inte bara det problematiska
    paret. Momentum-scannern, den dynamiska bevakningslistan och registret
    för nya listningar slutade alla fungera på grund av ett enda par.

    Lösningen: anropa KuCoins endpoint direkt och tolka svaret själva.
    Vi behöver bara fem fält, och de är entydiga i rådatan.

    Returnerar samma form som ccxt skulle gett:
        {"BTC/USDT": {"quoteVolume", "bid", "ask", "last", "percentage"}}
    """
    raw = None
    for method in ("publicGetMarketAllTickers", "public_get_market_alltickers"):
        fn = getattr(exchange, method, None)
        if fn:
            try:
                raw = fn()
                break
            except Exception:
                continue

    if not raw:
        # Sista utväg: låt ccxt försöka. Kraschar den får anroparen hantera det.
        return exchange.fetch_tickers()

    tickers = {}
    for t in (raw.get("data") or {}).get("ticker") or []:
        market_id = t.get("symbol") or ""
        if "-" not in market_id:
            continue
        base, quote = market_id.split("-", 1)
        symbol = f"{base}/{quote}"

        def num(key):
            v = t.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        change_rate = num("changeRate")
        tickers[symbol] = {
            "symbol": symbol,
            "quoteVolume": num("volValue"),   # 24h-omsättning i quote-valutan
            "bid": num("buy"),
            "ask": num("sell"),
            "last": num("last"),
            "percentage": change_rate * 100 if change_rate is not None else None,
        }

    return tickers
