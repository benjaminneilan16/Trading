"""
Korrelationsanalys — hindrar falsk riskspridning.

PROBLEMET DETTA LÖSER: fem positioner känns som riskspridning. Men om
alla fem rör sig likadant har du i praktiken EN position med fem gånger
storleken. BTC, ETH och SOL har historiskt korrelerat runt 0,8–0,9 —
går BTC ner tar de andra oftast med sig.

Riskhanteringen räknade tidigare bara antal positioner och total
exponering. Den kunde alltså godkänna fem positioner som alla är samma
vad, och rapportera det som väl diversifierat.

Korrelation mäts som Pearsons r på procentuella prisförändringar
(inte på priserna själva — det ger falskt höga värden för allt som
trendar åt samma håll).

    1,0  = rör sig identiskt
    0,0  = ingen relation
   -1,0  = rör sig tvärtom
"""
import logging

logger = logging.getLogger("correlation")

# Över detta räknas två tillgångar som "samma vad"
HIGH_CORRELATION = 0.75


def _returns(closes: list[float]) -> list[float]:
    """Procentuella förändringar, inte absoluta priser."""
    return [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1] != 0
    ]


def pearson(a: list[float], b: list[float]) -> float | None:
    n = min(len(a), len(b))
    if n < 20:
        return None
    a, b = a[-n:], b[-n:]

    mean_a = sum(a) / n
    mean_b = sum(b) / n

    cov = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)

    denom = (var_a * var_b) ** 0.5
    if denom == 0:
        return None
    return cov / denom


def correlation_matrix(symbols: list[str], timeframe: str = "5m",
                       limit: int = 200) -> dict:
    """Korrelation mellan alla par av symboler."""
    from db import get_ohlcv

    series = {}
    for sym in symbols:
        rows = get_ohlcv(sym, timeframe, limit=limit)
        if len(rows) >= 30:
            series[sym] = _returns([float(r["close"]) for r in rows])

    matrix = {}
    pairs = []
    names = list(series.keys())

    for i, a in enumerate(names):
        matrix[a] = {}
        for b in names:
            if a == b:
                matrix[a][b] = 1.0
                continue
            r = pearson(series[a], series[b])
            matrix[a][b] = round(r, 3) if r is not None else None
            if r is not None and names.index(b) > i:
                pairs.append({"a": a, "b": b, "correlation": round(r, 3)})

    pairs.sort(key=lambda p: abs(p["correlation"]), reverse=True)
    highly_correlated = [p for p in pairs if abs(p["correlation"]) >= HIGH_CORRELATION]

    return {
        "matrix": matrix,
        "pairs": pairs,
        "highly_correlated": highly_correlated,
        "threshold": HIGH_CORRELATION,
        "note": (
            f"{len(highly_correlated)} par korrelerar över {HIGH_CORRELATION}. "
            "Att hålla flera av dessa samtidigt är inte riskspridning — "
            "det är en större position i samma sak."
        ) if highly_correlated else "Inga par korrelerar starkt just nu.",
    }


def check_correlation_conflict(new_symbol: str, held_symbols: list[str],
                                timeframe: str = "5m") -> dict:
    """
    Anropas av Risk Manager innan ett köp godkänns.

    Returnerar {"conflict": bool, "reason": str, "worst": ...}
    """
    if not held_symbols:
        return {"conflict": False, "reason": ""}

    from db import get_ohlcv

    rows = get_ohlcv(new_symbol, timeframe, limit=200)
    if len(rows) < 30:
        # Kan inte bedöma — släpp igenom hellre än att blockera på okunskap
        return {"conflict": False, "reason": "för lite data för korrelationskoll"}

    new_returns = _returns([float(r["close"]) for r in rows])

    worst_symbol = None
    worst_corr = 0.0

    for held in held_symbols:
        held_rows = get_ohlcv(held, timeframe, limit=200)
        if len(held_rows) < 30:
            continue
        held_returns = _returns([float(r["close"]) for r in held_rows])
        r = pearson(new_returns, held_returns)
        if r is not None and abs(r) > abs(worst_corr):
            worst_corr = r
            worst_symbol = held

    if worst_symbol and abs(worst_corr) >= HIGH_CORRELATION:
        return {
            "conflict": True,
            "reason": (
                f"{new_symbol} korrelerar {worst_corr:.2f} med {worst_symbol} "
                f"som redan hålls — det vore inte riskspridning"
            ),
            "worst_symbol": worst_symbol,
            "correlation": round(worst_corr, 3),
        }

    return {
        "conflict": False,
        "reason": "",
        "worst_symbol": worst_symbol,
        "correlation": round(worst_corr, 3) if worst_symbol else None,
    }
