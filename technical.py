"""
Technical Engine (Fas 4) — räknar ut tekniska indikatorer från candle-data.
Ingen extern beroende behövs (ingen pandas/ta-lib) — vi har inte så
mycket data (max ~200 candles) så vanlig Python räcker gott.

Alla funktioner tar en lista av floats (t.ex. closing-priser) och
returnerar en lista av samma längd, med `None` där det inte finns
tillräckligt med historik än för att räkna ut värdet.
"""
from typing import Optional


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponentiellt glidande medelvärde."""
    if len(values) < period:
        return [None] * len(values)

    k = 2 / (period + 1)
    result: list[Optional[float]] = [None] * (period - 1)
    sma = sum(values[:period]) / period
    result.append(sma)
    prev = sma
    for price in values[period:]:
        prev = price * k + prev * (1 - k)
        result.append(prev)
    return result


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    """Relative Strength Index — mäter om något är över-/underköpt (0-100)."""
    if len(values) < period + 1:
        return [None] * len(values)

    result: list[Optional[float]] = [None] * period
    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def calc(avg_gain, avg_loss):
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    result.append(calc(avg_gain, avg_loss))

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        result.append(calc(avg_gain, avg_loss))

    return result


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    """
    MACD-linje (skillnad mellan snabb och långsam EMA) och dess
    signal-linje (EMA av MACD-linjen). Returnerar (macd_line, signal_line).
    """
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)

    macd_line: list[Optional[float]] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]

    # EMA av macd_line, men bara på de värden som inte är None
    valid = [v for v in macd_line if v is not None]
    none_count = len(macd_line) - len(valid)
    signal_valid = ema(valid, signal) if valid else []
    signal_line: list[Optional[float]] = [None] * none_count + signal_valid

    return macd_line, signal_line


def atr(candles: list[list], period: int = 14) -> Optional[float]:
    """
    Average True Range — mäter hur volatil en token är.

    Används för dynamisk stop loss: en token som normalt rör sig 5% per
    timme behöver en bredare stop än en som rör sig 0,5%, annars blir du
    utstoppad av vanligt brus istället för av en riktig vändning.

    candles: ccxt-format [[ts, open, high, low, close, volume], ...]
    Returnerar ATR i procent av senaste priset.
    """
    if len(candles) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i][2]
        low = candles[i][3]
        prev_close = candles[i - 1][4]
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    recent = true_ranges[-period:]
    atr_value = sum(recent) / len(recent)
    last_price = candles[-1][4]

    if last_price <= 0:
        return None
    return atr_value / last_price * 100


def latest_indicators(candles: list[dict]) -> dict:
    """
    Tar en lista av candle-dicts (från db.get_ohlcv, äldst->nyast) och
    returnerar de senaste värdena för varje indikator, redo att användas
    av strategy.py för att fatta ett beslut.
    """
    closes = [float(c["close"]) for c in candles]

    if len(closes) < 26:
        return {"ready": False, "reason": "för lite historik (behöver minst 26 candles)"}

    ema_fast_series = ema(closes, 12)
    ema_slow_series = ema(closes, 26)
    rsi_series = rsi(closes, 14)
    macd_line, macd_signal_line = macd(closes)

    return {
        "ready": True,
        "close": closes[-1],
        "ema_fast": ema_fast_series[-1],
        "ema_slow": ema_slow_series[-1],
        "ema_fast_prev": ema_fast_series[-2] if len(ema_fast_series) > 1 else None,
        "ema_slow_prev": ema_slow_series[-2] if len(ema_slow_series) > 1 else None,
        "rsi": rsi_series[-1],
        "macd": macd_line[-1],
        "macd_signal": macd_signal_line[-1],
        "macd_prev": macd_line[-2] if len(macd_line) > 1 else None,
        "macd_signal_prev": macd_signal_line[-2] if len(macd_signal_line) > 1 else None,
    }
