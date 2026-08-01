"""
Decision Engine (förenklad Fas 8) — väger samman tekniska signaler till
ett beslut: 'buy', 'sell', eller 'hold'.

VIKTIGT ATT FÖRSTÅ: detta är REGELBASERAT, inte en AI som "lär sig" eller
hittar på egna strategier. Den följer tre enkla, kända tekniska regler
och röstar. Det är exakt så här enkla kvant-strategier brukar se ut i
verkligheten innan man bygger på med maskininlärning — transparent och
justerbart, snarare än en svart låda.

Rösterna:
1. EMA-crossover (12 vs 26)   — trend
2. RSI (14)                   — över-/underköpt
3. MACD vs signallinje        — momentum

Varje röst ger +1 (bullish), -1 (bearish) eller 0 (neutral).
Summan avgör beslutet:
    score >= BUY_THRESHOLD   -> köp
    score <= SELL_THRESHOLD  -> sälj
    annars                   -> avvakta
"""
from technical import latest_indicators

BUY_THRESHOLD = 2
SELL_THRESHOLD = -2

RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70


def decide(candles: list[dict]) -> dict:
    """
    Tar candle-historik för en symbol och returnerar ett beslut:
    {"decision": "buy"|"sell"|"hold", "score": int, "reason": str, ...indikatorvärden}
    """
    ind = latest_indicators(candles)
    if not ind["ready"]:
        return {"decision": "hold", "score": 0, "reason": ind["reason"], **ind}

    score = 0
    reasons = []

    # 1. EMA-crossover: gick snabb EMA precis över/under långsam EMA?
    if ind["ema_fast_prev"] is not None and ind["ema_slow_prev"] is not None:
        crossed_up = ind["ema_fast_prev"] <= ind["ema_slow_prev"] and ind["ema_fast"] > ind["ema_slow"]
        crossed_down = ind["ema_fast_prev"] >= ind["ema_slow_prev"] and ind["ema_fast"] < ind["ema_slow"]
        if crossed_up:
            score += 1
            reasons.append("EMA12 korsade upp över EMA26 (bullish)")
        elif crossed_down:
            score -= 1
            reasons.append("EMA12 korsade ner under EMA26 (bearish)")
        elif ind["ema_fast"] > ind["ema_slow"]:
            reasons.append("EMA12 > EMA26 (uppåttrend, redan pågående)")
        else:
            reasons.append("EMA12 < EMA26 (nedåttrend, redan pågående)")

    # 2. RSI: över-/underköpt
    if ind["rsi"] is not None:
        if ind["rsi"] < RSI_OVERSOLD:
            score += 1
            reasons.append(f"RSI {ind['rsi']:.1f} (underköpt, kan studsa upp)")
        elif ind["rsi"] > RSI_OVERBOUGHT:
            score -= 1
            reasons.append(f"RSI {ind['rsi']:.1f} (överköpt, risk för nedgång)")
        else:
            reasons.append(f"RSI {ind['rsi']:.1f} (neutralt läge)")

    # 3. MACD vs signallinje
    if ind["macd"] is not None and ind["macd_signal"] is not None:
        if ind["macd"] > ind["macd_signal"]:
            score += 1
            reasons.append("MACD över signallinjen (positivt momentum)")
        else:
            score -= 1
            reasons.append("MACD under signallinjen (negativt momentum)")

    if score >= BUY_THRESHOLD:
        decision = "buy"
    elif score <= SELL_THRESHOLD:
        decision = "sell"
    else:
        decision = "hold"

    return {
        "decision": decision,
        "score": score,
        "reason": " | ".join(reasons),
        **ind,
    }
