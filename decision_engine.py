"""
AI Decision Engine (Fas 8) — väger samman alla signalkällor till ett beslut.

Detta är din ursprungliga vision: istället för nio bottar som var för sig
tittar på en sak, en motor som väger samman teknisk analys, order flow,
social hype och marknadsregim.

HUR VIKTERNA SATTES — och varför de inte är "optimerade":

Frestelsen är att söka fram de vikter som gett bäst resultat historiskt.
Det vore överanpassning i sin renaste form: med fyra vikter och tillräckligt
många försök hittar man alltid en kombination som ser lysande ut bakåt och
misslyckas framåt.

Vikterna här är istället satta efter hur pålitlig varje signal är i princip:

  Order flow   0,40  — mäter vad kapital FAKTISKT gör, inte vad priset gjort.
                       Svårast att manipulera, närmast orsaken.
  Teknisk      0,30  — välkänd och välanvänd, vilket också betyder att
                       kanten till stor del är bortarbetad.
  Regim        0,20  — säger inte köp/sälj, men avgör om övriga signaler
                       ska tas på allvar. En trendsignal i sidledes marknad
                       är brus.
  Social       0,10  — lägst vikt medvetet. När hypen syns har rörelsen
                       ofta börjat, och mycket av den är botar.

Vill du ändra dem: gör det, men ändra dem INTE baserat på hur resultatet
såg ut igår. Det är precis den fällan uppdelningen träning/test finns för
att undvika.
"""
import logging

logger = logging.getLogger("decision")

WEIGHTS = {
    "orderflow": 0.40,
    "technical": 0.30,
    "regime": 0.20,
    "social": 0.10,
}

BUY_THRESHOLD = 0.30
SELL_THRESHOLD = -0.25


# MACD-linjen och signallinjen konvergerar i jämna trender och kan då
# flimra över/under varandra på tusendelar. Ett gap måste vara minst så
# här stort (som andel av priset) för att räknas som en riktig signal.
MACD_DEADBAND_PCT = 0.02


def _technical_score(candles: list, regime_data: dict = None) -> tuple[float, str]:
    """
    Sammanvägd teknisk signal, normaliserad till -1..+1.

    Tar hänsyn till marknadsregimen, av ett viktigt skäl: RSI "överköpt"
    betyder helt olika saker i olika marknader. I en sidledes marknad är
    överköpt en säljsignal. I en stark trend kan något vara överköpt i
    veckor medan priset fortsätter upp — där är samma signal en fälla.
    """
    from technical import ema, rsi, macd, vwap

    closes = [c[4] for c in candles]
    if len(closes) < 60:
        return 0.0, "för lite historik"

    ema_fast = ema(closes, 12)
    ema_slow = ema(closes, 26)
    rsi_series = rsi(closes, 14)
    macd_line, signal_line = macd(closes)
    vwap_series = vwap(candles, 20)

    score = 0.0
    parts = []

    # Trend
    if ema_fast[-1] and ema_slow[-1]:
        if ema_fast[-1] > ema_slow[-1]:
            score += 0.3
            parts.append("EMA bullish")
        else:
            score -= 0.3
            parts.append("EMA bearish")

    # Momentum — med dödband, så mikroskopiska skillnader inte räknas
    if macd_line[-1] is not None and signal_line[-1] is not None:
        histogram = macd_line[-1] - signal_line[-1]
        gap_pct = abs(histogram) / closes[-1] * 100 if closes[-1] else 0

        if gap_pct < MACD_DEADBAND_PCT:
            parts.append("MACD neutral (linjerna sammanfaller)")
        elif histogram > 0:
            score += 0.25
            parts.append("MACD positiv")
        else:
            score -= 0.25
            parts.append("MACD negativ")

    # Över-/underköpt — tolkas olika beroende på regim
    is_trending = bool(
        regime_data
        and regime_data.get("available")
        and regime_data.get("regime") in ("trending_up", "trending")
    )

    r = rsi_series[-1]
    if r is not None:
        if r < 30:
            score += 0.25
            parts.append(f"RSI {r:.0f} underköpt")
        elif r > 70:
            if is_trending:
                # I en trend är överköpt ofta bara styrka, inte en topp.
                # Straffa lätt istället för att motarbeta trendsignalen.
                score -= 0.05
                parts.append(f"RSI {r:.0f} högt men trendande")
            else:
                score -= 0.25
                parts.append(f"RSI {r:.0f} överköpt")

    # Pris mot VWAP — handlas det över eller under snittet där volymen skett?
    if vwap_series[-1]:
        if closes[-1] > vwap_series[-1]:
            score += 0.2
            parts.append("över VWAP")
        else:
            score -= 0.2
            parts.append("under VWAP")

    return max(min(score, 1.0), -1.0), ", ".join(parts)


def _regime_multiplier(regime_data: dict, technical_score: float) -> tuple[float, str]:
    """
    Regimen ger inte köp/sälj — den avgör om övriga signaler ska lita på.

    En stark trendsignal i en sidledes marknad är oftast brus, och ska
    därför dämpas. Samma signal i en trendande marknad ska förstärkas.
    """
    if not regime_data or not regime_data.get("available"):
        return 0.0, "regim okänd"

    regime = regime_data["regime"]
    er = regime_data.get("efficiency_ratio", 0)

    if regime in ("trending_up", "trending"):
        # Trendande marknad: förstärk signaler som pekar med trenden
        return (0.5 if technical_score > 0 else -0.2), f"trendande (ER {er})"
    if regime == "trending_down":
        return -0.5, f"nedåttrend (ER {er})"
    if regime == "ranging":
        # Sidledes: dämpa trendsignaler, de är oftast falska här
        return (-0.3 if abs(technical_score) > 0.4 else 0.0), f"sidledes (ER {er})"
    return 0.0, f"blandat (ER {er})"


def decide(symbol: str, candles: list, flow: dict = None,
           hype: dict = None, regime_data: dict = None) -> dict:
    """
    Fattar ett beslut för en symbol genom att väga samman alla källor.

    Returnerar decision ('buy'/'sell'/'hold'), score, och en läsbar
    motivering per källa — så du kan se exakt varför.
    """
    components = {}
    reasons = []

    # --- Teknisk (regimmedveten) ---
    tech_score, tech_reason = _technical_score(candles, regime_data)
    components["technical"] = tech_score
    reasons.append(f"Teknisk {tech_score:+.2f} ({tech_reason})")

    # --- Order flow ---
    if flow and flow.get("available"):
        flow_score = flow["score"]
        components["orderflow"] = flow_score
        reasons.append(f"Order flow {flow_score:+.2f} ({flow['reason']})")
    else:
        components["orderflow"] = 0.0
        reasons.append("Order flow: ingen data")

    # --- Social ---
    if hype and hype.get("available"):
        social_score = hype["score"]
        components["social"] = social_score
        src = hype.get("sources", {}).get("reddit", {})
        reasons.append(
            f"Social {social_score:+.2f} ({src.get('recent_posts_6h', 0)} inlägg 6h, "
            f"{src.get('ratio', 0)}x normalt)"
        )
    else:
        components["social"] = 0.0
        reasons.append("Social: ej konfigurerat")

    # --- Regim ---
    regime_score, regime_reason = _regime_multiplier(regime_data, tech_score)
    components["regime"] = regime_score
    reasons.append(f"Regim {regime_score:+.2f} ({regime_reason})")

    # --- Sammanvägning ---
    total = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)
    total = max(min(total, 1.0), -1.0)

    if total >= BUY_THRESHOLD:
        decision = "buy"
    elif total <= SELL_THRESHOLD:
        decision = "sell"
    else:
        decision = "hold"

    return {
        "symbol": symbol,
        "decision": decision,
        "score": round(total, 3),
        "components": {k: round(v, 3) for k, v in components.items()},
        "weights": WEIGHTS,
        "contributions": {
            k: round(components[k] * WEIGHTS[k], 3) for k in WEIGHTS
        },
        "reason": " | ".join(reasons),
        "thresholds": {"buy": BUY_THRESHOLD, "sell": SELL_THRESHOLD},
    }
