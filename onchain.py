"""
On-chain features via DexScreener.

VARFÖR DETTA ÄR ANNORLUNDA ÄN ALLT ANNAT VI SAMLAT IN:

EMA, RSI, MACD och Bollinger räknar alla på samma prisserie. Att lägga
till en indikator till ger ingen ny information — bara samma data
ompaketerad. Det är en av anledningarna till att arenan visar
bruttorörelser kring noll.

DexScreener ger något genuint annat: vad som händer i DEX-poolerna.
Likviditet, antal köp mot antal sälj, market cap, och verklig tokenålder.
Det är information som inte går att räkna fram ur ett prisdiagram.

MEST VÄRDEFULLA FÄLTET: buy/sell-ratio. Antalet köptransaktioner mot
säljtransaktioner den senaste timmen är faktiskt on-chain order flow —
vad plånböcker gör, inte vad priset gjorde. Renare än vad vi kan
härleda från KuCoins trades.

VAD SOM INTE GÅR ATT FÅ HÄRIFRÅN: holders, holder growth, wallet
concentration, dev-innehav, smart money. Det kräver kedjeindexerare
(Helius, Bitquery) och är ett eget projekt.

TVÅ FALLGROPAR SOM HANTERAS I KODEN:

1. Symbolmatchning. "PEPE/USDT" på KuCoin — vilket kontrakt? Det finns
   dussintals tokens med samma ticker, många är bluffar. Vi väljer alltid
   paret med HÖGST LIKVIDITET och sparar kontraktsadressen, så att
   matchningen är stabil över tid och går att granska i efterhand.

2. Ingen historik. DexScreener ger bara ögonblicksbilder. Därför sparar
   vi varje mätning i databasen — förändringar (volymacceleration,
   likviditetstillväxt) räknas fram genom att jämföra mätningar.

API:t är gratis, kräver ingen nyckel, och tillåter 300 anrop/minut för
par-frågor. Vi ligger långt under det.
"""
import logging
import time
from datetime import datetime, timezone

import requests

from db import get_cursor

logger = logging.getLogger("onchain")

BASE_URL = "https://api.dexscreener.com/latest/dex"
TIMEOUT = 10

# Enkel takthållning — vi ligger långt under 300/min men vill ändå
# inte skicka anrop i en tät loop.
MIN_SECONDS_BETWEEN_CALLS = 0.3
_last_call = 0.0

# Minsta likviditet för att en matchning ska accepteras. Under detta är
# paret troligen inte samma token som handlas på KuCoin.
MIN_MATCH_LIQUIDITY_USD = 10_000


def _throttled_get(url: str, params: dict = None) -> dict | None:
    global _last_call
    wait = MIN_SECONDS_BETWEEN_CALLS - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()

    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 429:
            logger.warning("DexScreener rate limit — väntar")
            time.sleep(5)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("DexScreener-anrop misslyckades (%s): %s", url, e)
        return None


def ensure_tables():
    with get_cursor() as cur:
        # Karta från KuCoin-symbol till DEX-par. Sparas så vi slipper
        # söka om varje gång, och så att matchningen går att granska.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS token_dex_map (
                symbol         TEXT PRIMARY KEY,
                chain_id       TEXT,
                pair_address   TEXT,
                token_address  TEXT,
                dex_id         TEXT,
                matched_liquidity_usd NUMERIC,
                matched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
                match_failed   BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS token_features (
                id              BIGSERIAL PRIMARY KEY,
                symbol          TEXT        NOT NULL,
                ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
                chain_id        TEXT,
                pair_address    TEXT,
                price_usd       NUMERIC,
                liquidity_usd   NUMERIC,
                fdv             NUMERIC,
                market_cap      NUMERIC,
                volume_m5       NUMERIC,
                volume_h1       NUMERIC,
                volume_h6       NUMERIC,
                volume_h24      NUMERIC,
                buys_h1         INT,
                sells_h1        INT,
                buys_h24        INT,
                sells_h24       INT,
                buy_sell_ratio_h1  NUMERIC,
                buy_sell_ratio_h24 NUMERIC,
                price_change_m5  NUMERIC,
                price_change_h1  NUMERIC,
                price_change_h6  NUMERIC,
                price_change_h24 NUMERIC,
                pair_created_at TIMESTAMPTZ,
                age_days        NUMERIC,
                volume_acceleration NUMERIC,
                liquidity_change_pct NUMERIC
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_token_features_symbol_ts "
            "ON token_features (symbol, ts DESC)"
        )


def _base_symbol(symbol: str) -> str:
    return symbol.split("/")[0].split(":")[0].upper()


def find_dex_pair(symbol: str, force: bool = False) -> dict | None:
    """
    Hittar rätt DEX-par för en KuCoin-symbol.

    Matchningen är den känsliga delen: samma ticker kan finnas på
    dussintals kontrakt. Vi kräver exakt tickermatch OCH väljer det par
    som har högst likviditet, eftersom det nästan alltid är det riktiga.
    Resultatet sparas så att matchningen är stabil över tid.
    """
    ensure_tables()

    if not force:
        with get_cursor(commit=False) as cur:
            cur.execute(
                "SELECT chain_id, pair_address, token_address, dex_id, match_failed "
                "FROM token_dex_map WHERE symbol = %s",
                (symbol,),
            )
            row = cur.fetchone()
        if row:
            if row[4]:  # match_failed
                return None
            return {"chain_id": row[0], "pair_address": row[1],
                    "token_address": row[2], "dex_id": row[3]}

    base = _base_symbol(symbol)
    data = _throttled_get(f"{BASE_URL}/search", {"q": base})

    best = None
    if data:
        for pair in data.get("pairs") or []:
            bt = pair.get("baseToken") or {}
            if (bt.get("symbol") or "").upper() != base:
                continue
            liq = ((pair.get("liquidity") or {}).get("usd")) or 0
            if liq < MIN_MATCH_LIQUIDITY_USD:
                continue
            if best is None or liq > best["liq"]:
                best = {
                    "liq": liq,
                    "chain_id": pair.get("chainId"),
                    "pair_address": pair.get("pairAddress"),
                    "token_address": bt.get("address"),
                    "dex_id": pair.get("dexId"),
                }

    with get_cursor() as cur:
        if best:
            cur.execute(
                """
                INSERT INTO token_dex_map
                    (symbol, chain_id, pair_address, token_address, dex_id,
                     matched_liquidity_usd, match_failed)
                VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (symbol) DO UPDATE SET
                    chain_id = EXCLUDED.chain_id,
                    pair_address = EXCLUDED.pair_address,
                    token_address = EXCLUDED.token_address,
                    dex_id = EXCLUDED.dex_id,
                    matched_liquidity_usd = EXCLUDED.matched_liquidity_usd,
                    match_failed = FALSE,
                    matched_at = now()
                """,
                (symbol, best["chain_id"], best["pair_address"],
                 best["token_address"], best["dex_id"], best["liq"]),
            )
            logger.info("Matchade %s -> %s på %s (likviditet %.0f USD)",
                        symbol, best["dex_id"], best["chain_id"], best["liq"])
        else:
            # Spara även misslyckade matchningar, annars söker vi om
            # varje cykel för tokens som helt enkelt inte finns på DEX.
            cur.execute(
                """
                INSERT INTO token_dex_map (symbol, match_failed)
                VALUES (%s, TRUE)
                ON CONFLICT (symbol) DO UPDATE SET
                    match_failed = TRUE, matched_at = now()
                """,
                (symbol,),
            )
            logger.debug("Ingen DEX-match för %s", symbol)

    return best


def parse_pair(pair: dict) -> dict:
    """Plockar ut de features vi bryr oss om ur DexScreeners parobjekt."""
    txns = pair.get("txns") or {}
    vol = pair.get("volume") or {}
    change = pair.get("priceChange") or {}
    liq = pair.get("liquidity") or {}

    def n(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    h1 = txns.get("h1") or {}
    h24 = txns.get("h24") or {}
    buys_h1, sells_h1 = h1.get("buys"), h1.get("sells")
    buys_h24, sells_h24 = h24.get("buys"), h24.get("sells")

    def ratio(b, s):
        if b is None or s is None:
            return None
        total = b + s
        return (b / total) if total else None

    created_ms = pair.get("pairCreatedAt")
    created = (datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
               if created_ms else None)
    age_days = ((datetime.now(timezone.utc) - created).total_seconds() / 86400
                if created else None)

    return {
        "chain_id": pair.get("chainId"),
        "pair_address": pair.get("pairAddress"),
        "price_usd": n(pair.get("priceUsd")),
        "liquidity_usd": n(liq.get("usd")),
        "fdv": n(pair.get("fdv")),
        "market_cap": n(pair.get("marketCap")),
        "volume_m5": n(vol.get("m5")),
        "volume_h1": n(vol.get("h1")),
        "volume_h6": n(vol.get("h6")),
        "volume_h24": n(vol.get("h24")),
        "buys_h1": buys_h1,
        "sells_h1": sells_h1,
        "buys_h24": buys_h24,
        "sells_h24": sells_h24,
        # 0.5 = balanserat, över 0.5 = fler köp än sälj
        "buy_sell_ratio_h1": ratio(buys_h1, sells_h1),
        "buy_sell_ratio_h24": ratio(buys_h24, sells_h24),
        "price_change_m5": n(change.get("m5")),
        "price_change_h1": n(change.get("h1")),
        "price_change_h6": n(change.get("h6")),
        "price_change_h24": n(change.get("h24")),
        "pair_created_at": created,
        "age_days": age_days,
    }


def fetch_features(symbol: str) -> dict | None:
    """Hämtar aktuella features för en symbol och sparar en mätning."""
    mapping = find_dex_pair(symbol)
    if not mapping:
        return None

    data = _throttled_get(
        f"{BASE_URL}/pairs/{mapping['chain_id']}/{mapping['pair_address']}"
    )
    if not data:
        return None

    pairs = data.get("pairs") or data.get("pair")
    if isinstance(pairs, dict):
        pairs = [pairs]
    if not pairs:
        return None

    f = parse_pair(pairs[0])
    f["symbol"] = symbol

    # Förändring sedan förra mätningen — det är här den verkliga
    # signalen finns. En ögonblicksbild säger lite; att volymen
    # tredubblats på en timme säger mycket.
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT volume_h1, liquidity_usd FROM token_features "
            "WHERE symbol = %s ORDER BY ts DESC LIMIT 1",
            (symbol,),
        )
        prev = cur.fetchone()

    vol_accel = None
    liq_change = None
    if prev:
        prev_vol, prev_liq = prev
        if prev_vol and float(prev_vol) > 0 and f["volume_h1"]:
            vol_accel = f["volume_h1"] / float(prev_vol)
        if prev_liq and float(prev_liq) > 0 and f["liquidity_usd"]:
            liq_change = (f["liquidity_usd"] - float(prev_liq)) / float(prev_liq) * 100

    f["volume_acceleration"] = vol_accel
    f["liquidity_change_pct"] = liq_change

    _store(f)
    return f


def _store(f: dict):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO token_features
                (symbol, chain_id, pair_address, price_usd, liquidity_usd, fdv,
                 market_cap, volume_m5, volume_h1, volume_h6, volume_h24,
                 buys_h1, sells_h1, buys_h24, sells_h24,
                 buy_sell_ratio_h1, buy_sell_ratio_h24,
                 price_change_m5, price_change_h1, price_change_h6, price_change_h24,
                 pair_created_at, age_days, volume_acceleration, liquidity_change_pct)
            VALUES (%(symbol)s, %(chain_id)s, %(pair_address)s, %(price_usd)s,
                    %(liquidity_usd)s, %(fdv)s, %(market_cap)s, %(volume_m5)s,
                    %(volume_h1)s, %(volume_h6)s, %(volume_h24)s,
                    %(buys_h1)s, %(sells_h1)s, %(buys_h24)s, %(sells_h24)s,
                    %(buy_sell_ratio_h1)s, %(buy_sell_ratio_h24)s,
                    %(price_change_m5)s, %(price_change_h1)s, %(price_change_h6)s,
                    %(price_change_h24)s, %(pair_created_at)s, %(age_days)s,
                    %(volume_acceleration)s, %(liquidity_change_pct)s)
            """,
            f,
        )


def latest_features(symbol: str, max_age_minutes: int = 30) -> dict | None:
    """
    Senaste sparade features för en symbol, om de inte är för gamla.

    Används vid entry för att logga vilka förutsättningar som rådde när
    affären togs — utan att göra ett API-anrop mitt i en botcykel.
    """
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM token_features WHERE symbol = %s "
            "AND ts >= now() - INTERVAL '%s minutes' ORDER BY ts DESC LIMIT 1"
            % ("%s", max_age_minutes),
            (symbol,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [c.name for c in cur.description]
    return dict(zip(cols, row))


def collect_for_symbols(symbols: list[str]) -> dict:
    """Samlar features för en lista symboler. Körs på schema av motorn."""
    ensure_tables()
    collected, failed = 0, 0
    for sym in symbols:
        try:
            if fetch_features(sym):
                collected += 1
            else:
                failed += 1
        except Exception as e:
            logger.error("Feature-hämtning misslyckades för %s: %s", sym, e)
            failed += 1
    logger.info("On-chain features: %d hämtade, %d utan match", collected, failed)
    return {"collected": collected, "no_match": failed}


def coverage_stats() -> dict:
    """Hur många symboler har vi lyckats matcha mot DEX-par?"""
    ensure_tables()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT COUNT(*) FROM token_dex_map WHERE match_failed = FALSE")
        matched = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM token_dex_map WHERE match_failed = TRUE")
        unmatched = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM token_features")
        snapshots = cur.fetchone()[0]
        cur.execute("SELECT COUNT(DISTINCT symbol) FROM token_features")
        symbols_with_data = cur.fetchone()[0]
    return {
        "matched_to_dex": matched,
        "no_dex_match": unmatched,
        "snapshots_stored": snapshots,
        "symbols_with_data": symbols_with_data,
        "note": ("Tokens utan DEX-match handlas troligen bara på centraliserade "
                 "börser, eller har en ticker som inte gick att matcha säkert."),
    }
