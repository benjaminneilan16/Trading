"""
Regimdetektering — vilken sorts marknad är det just nu?

VARFÖR DETTA ÄR VIKTIGT: ingen strategi fungerar i alla marknadslägen.
Trendföljare (EMA-cross, Donchian, breakouts) tjänar pengar när priset
rör sig rakt, och blöder när det går sidledes. Mean reversion (RSI,
Bollinger-reversion) gör precis tvärtom.

När din arena visar att `donchian_breakout` leder säger det inte bara
"donchian är bra" — det säger "marknaden trendar just nu". Utan
regiminformation tolkar man tur som skicklighet.

TRE MÅTT:

1. Efficiency Ratio (Kaufman) — hur rak rörelsen är
   Hög = trend, låg = brus. Detta är huvudmåttet.

2. Volatilitet (ATR i procent) — hur mycket det svänger
   Hög volatilitet kräver bredare stops, oavsett riktning.

3. Riktning (pris mot långt glidande medelvärde)
   Upp, ner, eller platt.

Kombinationen ger fyra regimer, och för varje regim vet vi vilken
strategifamilj som HISTORISKT brukar passa. Det är ingen garanti —
men det förklarar resultaten i arenan.
"""
import logging

from technical import efficiency_ratio, atr, ema

logger = logging.getLogger("regime")

# Efficiency Ratio-trösklar
ER_TRENDING = 0.35      # över detta: tydlig trend
ER_RANGING = 0.15       # under detta: sidledes/brus

# Volatilitet (ATR i procent av priset)
VOL_HIGH = 2.0
VOL_LOW = 0.5


def detect_regime(candles: list) -> dict:
    """
    candles: ccxt-format [[ts, o, h, l, c, v], ...], helst minst 100 st.
    """
    if len(candles) < 60:
        return {"available": False, "reason": f"För få candles ({len(candles)}), behöver 60+"}

    closes = [c[4] for c in candles]

    er = efficiency_ratio(closes, period=20)
    volatility = atr(candles[-30:], period=14)
    trend_ema = ema(closes, 50)

    if er is None or volatility is None or trend_ema[-1] is None:
        return {"available": False, "reason": "Kunde inte beräkna indikatorer"}

    price = closes[-1]
    ema_value = trend_ema[-1]
    distance_from_ema_pct = (price - ema_value) / ema_value * 100

    # --- Riktning ---
    if distance_from_ema_pct > 0.5:
        direction = "up"
    elif distance_from_ema_pct < -0.5:
        direction = "down"
    else:
        direction = "flat"

    # --- Regim ---
    if er >= ER_TRENDING:
        regime = "trending_up" if direction == "up" else (
            "trending_down" if direction == "down" else "trending"
        )
    elif er <= ER_RANGING:
        regime = "ranging"
    else:
        regime = "mixed"

    if volatility >= VOL_HIGH:
        volatility_label = "hög"
    elif volatility <= VOL_LOW:
        volatility_label = "låg"
    else:
        volatility_label = "normal"

    return {
        "available": True,
        "regime": regime,
        "direction": direction,
        "efficiency_ratio": round(er, 3),
        "volatility_atr_pct": round(volatility, 2),
        "volatility_label": volatility_label,
        "distance_from_ema50_pct": round(distance_from_ema_pct, 2),
        "description": _describe(regime, volatility_label),
        "favors": _favors(regime),
        "avoid": _avoid(regime),
    }


def _describe(regime: str, vol: str) -> str:
    base = {
        "trending_up": "Marknaden trendar uppåt — priset rör sig relativt rakt.",
        "trending_down": "Marknaden trendar nedåt.",
        "trending": "Tydlig trend, men riktningen är inte entydig mot EMA50.",
        "ranging": "Sidledes marknad — priset rör sig mycket men kommer ingenstans.",
        "mixed": "Blandat läge, varken tydlig trend eller tydligt sidledes.",
    }.get(regime, "Okänd regim.")
    return f"{base} Volatiliteten är {vol}."


def _favors(regime: str) -> list[str]:
    """Vilka strategifamiljer som historiskt brukar passa denna regim."""
    return {
        "trending_up": ["ema_cross", "macd_cross", "donchian_breakout",
                        "bollinger_breakout", "trend_filtered_momentum"],
        "trending_down": ["(inga — bottarna handlar bara långt)"],
        "trending": ["ema_cross", "donchian_breakout"],
        "ranging": ["rsi_mean_reversion", "bollinger_reversion"],
        "mixed": ["order_flow_pressure", "whale_follow"],
    }.get(regime, [])


def _avoid(regime: str) -> list[str]:
    return {
        "trending_up": ["rsi_mean_reversion", "bollinger_reversion"],
        "trending_down": ["alla långstrategier"],
        "ranging": ["ema_cross", "donchian_breakout", "bollinger_breakout"],
        "mixed": [],
    }.get(regime, [])


def regime_for_symbols(symbols: list[str], timeframe: str = "5m") -> dict:
    """Regim per symbol, plus en samlad bild."""
    from db import get_ohlcv

    results = {}
    for sym in symbols:
        rows = get_ohlcv(sym, timeframe, limit=150)
        if len(rows) < 60:
            results[sym] = {"available": False, "reason": "för lite data"}
            continue
        candles = [
            [int(r["ts"].timestamp() * 1000), float(r["open"]), float(r["high"]),
             float(r["low"]), float(r["close"]), float(r["volume"])]
            for r in rows
        ]
        results[sym] = detect_regime(candles)

    available = [v for v in results.values() if v.get("available")]
    if not available:
        return {"symbols": results, "overall": None}

    # Samlad bild: vilken regim är vanligast?
    counts = {}
    for v in available:
        counts[v["regime"]] = counts.get(v["regime"], 0) + 1
    dominant = max(counts, key=counts.get)

    avg_er = sum(v["efficiency_ratio"] for v in available) / len(available)

    return {
        "symbols": results,
        "overall": {
            "dominant_regime": dominant,
            "agreement": f"{counts[dominant]} av {len(available)} symboler",
            "avg_efficiency_ratio": round(avg_er, 3),
            "description": _describe(dominant, "varierande"),
            "favors": _favors(dominant),
            "avoid": _avoid(dominant),
        },
    }
