"""
Nya listningar — hitta tokens som precis kommit till KuCoin.

VARFÖR: nya listningar rör sig mer än etablerade tokens. Det finns ingen
historik att förankra priset i, färre som redan äger, och listningen i sig
är en händelse som drar uppmärksamhet.

VARFÖR DET ÄR FARLIGT: exakt samma egenskaper gör dem till det bästa
jaktmarkerna för pump-and-dump. Spreadarna är bredare, likviditeten
tunnare, och "listningspumpen" vänder ofta hårt inom timmar. Du kan vara
den som köper toppen från någon som fick tokens gratis före listningen.

Riskhanteringen gäller fullt ut här, och trösklarna är medvetet strängare
än för vanliga tokens.

HUR ÅLDERN MÄTS — utan att KuCoin berättar den:

KuCoins API säger inte när ett par listades. Men dagliga candles finns
bara från listningsdagen och framåt. Hämtar man 200 dagliga candles och
får tillbaka 18, är token ungefär 18 dagar gammal. Enkelt, och fungerar
retroaktivt utan att vi behöver ha samlat data innan.

TVÅ SÄTT ATT HITTA NYA:

1. Åldersmätning (ovan) — hittar unga tokens direkt, även sådana som
   listades innan vi började lyssna.
2. Registret — vi sparar alla symboler vi sett. Dyker en ny upp som inte
   finns i registret är den listad just nu. Detta ger den snabbaste
   signalen, men bara framåt i tiden.
"""
import logging
from datetime import datetime, timezone

from db import get_cursor

logger = logging.getLogger("newlistings")

# En token räknas som "ny" upp till så här många dagar
NEW_TOKEN_MAX_AGE_DAYS = 30
# "Mycket ny" — extra bonus i scannern, men också extra försiktighet
VERY_NEW_MAX_AGE_DAYS = 7

# Likviditetskrav för nya tokens. STRÄNGARE än för vanliga tokens,
# eftersom slippage på en illikvid ny listning kan äta hela vinsten.
MIN_NEW_TOKEN_VOLUME_USDT = 500_000
MAX_NEW_TOKEN_SPREAD_PCT = 0.8

# Hur många okontrollerade symboler som åldersbestäms per cykel.
# Varje kontroll är ett API-anrop, så vi sprider ut dem över tid istället
# för att göra 800 anrop på en gång.
AGE_CHECKS_PER_CYCLE = 15


def ensure_registry():
    with get_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS token_registry (
                symbol          TEXT PRIMARY KEY,
                first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
                age_days        INT,
                age_checked_at  TIMESTAMPTZ,
                quote_volume_24h NUMERIC,
                is_new_listing  BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )


def measure_token_age(exchange, symbol: str) -> int | None:
    """
    Uppskattar tokens ålder i dagar genom att räkna dagliga candles.
    Returnerar None om det inte gick att avgöra.
    """
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=400)
    except Exception as e:
        logger.debug("Kunde inte hämta dagscandles för %s: %s", symbol, e)
        return None

    if not candles:
        return None

    # Om vi fick tillbaka färre än vi bad om är det hela historiken
    return len(candles)


def sync_registry(exchange) -> dict:
    """
    Ett API-anrop hämtar alla marknader. Nya symboler läggs till i
    registret; sådana som dyker upp EFTER att registret fyllts är
    genuint nylistade.
    """
    ensure_registry()

    try:
        from collectors.exchange import fetch_all_tickers
        tickers = fetch_all_tickers(exchange)
    except Exception as e:
        logger.error("Kunde inte hämta tickers: %s", e)
        return {"error": str(e)}

    usdt_symbols = {
        s: t for s, t in tickers.items()
        if s.endswith("/USDT") and t.get("quoteVolume")
    }

    with get_cursor(commit=False) as cur:
        cur.execute("SELECT symbol FROM token_registry")
        known = {r[0] for r in cur.fetchall()}

    # Är registret tomt är detta första körningen — då är inget "nytt",
    # bara okänt. Annars skulle allt flaggas som nylistat.
    first_run = len(known) == 0

    new_symbols = [s for s in usdt_symbols if s not in known]

    with get_cursor() as cur:
        for sym in new_symbols:
            cur.execute(
                """
                INSERT INTO token_registry (symbol, quote_volume_24h, is_new_listing)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol) DO NOTHING
                """,
                (sym, usdt_symbols[sym].get("quoteVolume"), not first_run),
            )
        # Uppdatera volym för befintliga
        for sym, t in usdt_symbols.items():
            cur.execute(
                "UPDATE token_registry SET quote_volume_24h = %s WHERE symbol = %s",
                (t.get("quoteVolume"), sym),
            )

    if new_symbols and not first_run:
        logger.info("NYA LISTNINGAR upptäckta: %s", ", ".join(new_symbols[:10]))

    return {
        "total_symbols": len(usdt_symbols),
        "new_symbols": new_symbols if not first_run else [],
        "first_run": first_run,
        "registry_size": len(known) + len(new_symbols),
    }


def check_pending_ages(exchange) -> int:
    """
    Åldersbestämmer några symboler i taget. Prioriterar de med hög volym —
    en okänd token utan volym är inte värd ett API-anrop.
    """
    ensure_registry()

    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT symbol FROM token_registry
            WHERE age_checked_at IS NULL
              AND quote_volume_24h >= %s
            ORDER BY quote_volume_24h DESC
            LIMIT %s
            """,
            (MIN_NEW_TOKEN_VOLUME_USDT, AGE_CHECKS_PER_CYCLE),
        )
        pending = [r[0] for r in cur.fetchall()]

    checked = 0
    for sym in pending:
        age = measure_token_age(exchange, sym)
        with get_cursor() as cur:
            cur.execute(
                "UPDATE token_registry SET age_days = %s, age_checked_at = now() "
                "WHERE symbol = %s",
                (age, sym),
            )
        checked += 1

    if checked:
        logger.info("Åldersbestämde %d symboler", checked)
    return checked


def get_new_tokens(max_age_days: int = NEW_TOKEN_MAX_AGE_DAYS) -> list[dict]:
    """Alla kända tokens under angiven ålder, sorterat på ålder."""
    ensure_registry()
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT symbol, age_days, first_seen, quote_volume_24h, is_new_listing
            FROM token_registry
            WHERE age_days IS NOT NULL
              AND age_days <= %s
              AND quote_volume_24h >= %s
            ORDER BY age_days ASC
            """,
            (max_age_days, MIN_NEW_TOKEN_VOLUME_USDT),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def get_age(symbol: str) -> int | None:
    """Ålder för en enskild symbol, om vi känner till den."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT age_days FROM token_registry WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else None


def newness_bonus(symbol: str) -> tuple[float, str]:
    """
    Bonuspoäng till scannern för unga tokens.

    Medvetet begränsad: ungdom i sig är ingen köpsignal, bara en
    förstärkning av en signal som redan finns. En ny token utan
    volymspik är fortfarande ointressant.
    """
    age = get_age(symbol)
    if age is None:
        return 0.0, ""

    if age <= VERY_NEW_MAX_AGE_DAYS:
        return 1.0, f"MYCKET NY ({age} dagar)"
    if age <= NEW_TOKEN_MAX_AGE_DAYS:
        return 0.5, f"ny ({age} dagar)"
    return 0.0, ""


def registry_stats() -> dict:
    ensure_registry()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT COUNT(*) FROM token_registry")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM token_registry WHERE age_days IS NOT NULL")
        aged = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM token_registry WHERE age_days IS NOT NULL AND age_days <= %s",
            (NEW_TOKEN_MAX_AGE_DAYS,),
        )
        new_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM token_registry WHERE is_new_listing = TRUE")
        detected_live = cur.fetchone()[0]

    return {
        "total_symbols_known": total,
        "age_measured": aged,
        "age_pending": total - aged,
        "new_tokens_under_30d": new_count,
        "detected_as_live_listing": detected_live,
        "thresholds": {
            "new_max_age_days": NEW_TOKEN_MAX_AGE_DAYS,
            "very_new_max_age_days": VERY_NEW_MAX_AGE_DAYS,
            "min_volume_usdt": MIN_NEW_TOKEN_VOLUME_USDT,
            "max_spread_pct": MAX_NEW_TOKEN_SPREAD_PCT,
        },
    }
