"""
Momentum Scanner — letar efter tokens som BÖRJAR röra sig, inte de som
redan rusat färdigt.

Strategin bakom: en riktig rörelse föregås nästan alltid av att volymen
ökar kraftigt jämfört med tokenens EGEN normala volym. Vi letar alltså
inte efter "hög volym" (då hittar vi bara BTC varje gång) utan efter
"ovanligt hög volym för just den här token".

Två steg, för att inte överbelasta KuCoins API:
  Steg 1 (billigt): ett enda anrop hämtar ticker för ALLA marknader.
                    Vi filtrerar bort illikvida skräp-par direkt.
  Steg 2 (dyrare):  bara för de mest lovande kandidaterna hämtar vi
                    candles och räknar ut volymspik + prisacceleration.

Varje kandidat får en score. Höga scores är kandidater för snabb entry.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("scanner")

# --- Filter för steg 1 -----------------------------------------------------
# Under den här 24h-volymen är paret för illikvitt: du kommer inte ut ur
# positionen utan att förlora mer på slippage än du tjänar på rörelsen.
MIN_24H_QUOTE_VOLUME = 200_000     # USDT
# Över den här är token för stor för att "rusa" på det sätt vi letar efter.
MAX_24H_QUOTE_VOLUME = 50_000_000  # USDT

# Max spread i procent — bred spread = dyrt att gå in och ut snabbt
MAX_SPREAD_PCT = 1.0

# Hur många kandidater från steg 1 som går vidare till steg 2
TOP_CANDIDATES = 40

# --- Trösklar för steg 2 ---------------------------------------------------
# Hur många gånger normal volym den senaste candlen måste ha
VOLUME_SPIKE_MIN = 2.5
# Hur mycket priset måste ha rört sig senaste 15 min för att räknas som "på gång"
PRICE_CHANGE_MIN_PCT = 1.0
# Om priset redan gått upp mer än så här har vi missat tåget — hoppa över
PRICE_CHANGE_MAX_PCT = 15.0


def scan_stage1(exchange) -> list[dict]:
    """
    Ett API-anrop, alla marknader. Returnerar de mest lovande USDT-paren
    baserat på 24h-volym och spread.
    """
    tickers = exchange.fetch_tickers()
    candidates = []

    for symbol, t in tickers.items():
        # Bara spot-par mot USDT
        if not symbol.endswith("/USDT"):
            continue

        quote_volume = t.get("quoteVolume")
        bid, ask = t.get("bid"), t.get("ask")
        if not quote_volume or not bid or not ask:
            continue

        if not (MIN_24H_QUOTE_VOLUME <= quote_volume <= MAX_24H_QUOTE_VOLUME):
            continue

        spread_pct = (ask - bid) / bid * 100
        if spread_pct > MAX_SPREAD_PCT:
            continue

        candidates.append({
            "symbol": symbol,
            "quote_volume_24h": quote_volume,
            "spread_pct": spread_pct,
            "last_price": t.get("last"),
            "change_24h_pct": t.get("percentage"),
        })

    # Sortera så att par som redan visar dagsrörelse men inte är extrema
    # hamnar högst — de är oftast tidigast i en rörelse.
    candidates.sort(
        key=lambda c: abs(c.get("change_24h_pct") or 0),
        reverse=True,
    )
    logger.info("Scanner steg 1: %d kandidater efter filtrering", len(candidates))
    return candidates[:TOP_CANDIDATES]


def analyze_candidate(exchange, symbol: str) -> dict | None:
    """
    Steg 2 för EN symbol: hämtar candles och letar efter volymspik +
    tidig prisrörelse. Returnerar None om datan inte räcker.
    """
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe="5m", limit=30)
    except Exception as e:
        logger.debug("Kunde inte hämta candles för %s: %s", symbol, e)
        return None

    if len(candles) < 20:
        return None

    volumes = [c[5] for c in candles]
    closes = [c[4] for c in candles]

    # Jämför senaste candlens volym mot snittet av de föregående
    recent_volume = volumes[-1]
    baseline = sum(volumes[-20:-1]) / 19
    if baseline <= 0:
        return None
    volume_ratio = recent_volume / baseline

    # Prisrörelse senaste 15 min (3 st 5m-candles)
    price_change_pct = (closes[-1] - closes[-4]) / closes[-4] * 100

    # Prisacceleration: rör det sig SNABBARE nu än nyss?
    prev_change_pct = (closes[-4] - closes[-7]) / closes[-7] * 100
    accelerating = price_change_pct > prev_change_pct

    # --- Score ---
    score = 0.0
    reasons = []

    if volume_ratio >= VOLUME_SPIKE_MIN:
        score += min(volume_ratio / VOLUME_SPIKE_MIN, 3.0)  # tak vid 3 poäng
        reasons.append(f"volym {volume_ratio:.1f}x normalt")

    if PRICE_CHANGE_MIN_PCT <= price_change_pct <= PRICE_CHANGE_MAX_PCT:
        score += 1.0
        reasons.append(f"pris +{price_change_pct:.1f}% på 15m")
    elif price_change_pct > PRICE_CHANGE_MAX_PCT:
        # Redan rusat — vi är sent på bollen, straffa hårt
        score -= 2.0
        reasons.append(f"redan +{price_change_pct:.1f}% (troligen för sent)")

    if accelerating:
        score += 0.5
        reasons.append("accelererande")

    return {
        "symbol": symbol,
        "volume_ratio": volume_ratio,
        "price_change_15m_pct": price_change_pct,
        "accelerating": accelerating,
        "score": score,
        "last_price": closes[-1],
        "reason": ", ".join(reasons) if reasons else "ingen tydlig signal",
        "ts": datetime.now(timezone.utc),
    }


def scan(exchange, min_score: float = 2.0) -> list[dict]:
    """
    Full scan: steg 1 + steg 2. Returnerar kandidater sorterade på score,
    bara de som passerar min_score.
    """
    stage1 = scan_stage1(exchange)
    results = []

    for c in stage1:
        analysis = analyze_candidate(exchange, c["symbol"])
        if analysis is None:
            continue
        analysis["quote_volume_24h"] = c["quote_volume_24h"]
        analysis["spread_pct"] = c["spread_pct"]
        results.append(analysis)

    results.sort(key=lambda r: r["score"], reverse=True)
    hits = [r for r in results if r["score"] >= min_score]
    logger.info("Scanner steg 2: %d analyserade, %d över tröskeln", len(results), len(hits))
    return hits
