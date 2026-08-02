"""
Strategibibliotek — de vanligaste metoderna inom teknisk handel, byggda
med ett gemensamt gränssnitt så att lab.py kan testa dem alla likadant.

VIKTIGT FÖR PRESTANDA: varje strategi räknar ut sina indikatorer EN gång
över hela serien i prepare(), och signal() slår sedan bara upp värdet för
en given bar. Den gamla backtesten räknade om indikatorerna för varje bar,
vilket är O(n²) — det här är O(n). På 1000 candles är skillnaden ungefär
100x snabbare.

Att indikatorerna räknas i förväg introducerar INTE lookahead bias: en EMA
vid bar i beror bara på barer <= i per konstruktion. Loopen i lab.py får
ändå aldrig titta framåt i prislistan.

Varje strategi definierar:
    name        — läsbart namn
    param_grid  — parametrar som ska sökas igenom
    prepare()   — räknar ut indikatorserier en gång
    signal(i)   — 'buy' / 'sell' / 'hold' för bar i
    exit_mode   — 'rules'  = exit via take profit/stop/trailing/time
                  'signal' = exit när strategin säger 'sell'
"""
from technical import ema, rsi, macd


def _sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * (period - 1)
    if len(values) < period:
        return [None] * len(values)
    running = sum(values[:period])
    out.append(running / period)
    for i in range(period, len(values)):
        running += values[i] - values[i - period]
        out.append(running / period)
    return out


def _stddev(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * (period - 1)
    if len(values) < period:
        return [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        var = sum((v - mean) ** 2 for v in window) / period
        out.append(var ** 0.5)
    return out


class Strategy:
    name = "base"
    param_grid: dict = {}
    exit_mode = "rules"

    def __init__(self, **params):
        self.params = params

    def prepare(self, candles: list):
        raise NotImplementedError

    def signal(self, i: int) -> str:
        raise NotImplementedError

    def describe(self) -> str:
        p = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({p})" if p else self.name


# ---------------------------------------------------------------------------
# 1. EMA-crossover — klassisk trendföljning
# ---------------------------------------------------------------------------

class EmaCross(Strategy):
    name = "ema_cross"
    param_grid = {"fast": [8, 12, 21], "slow": [21, 26, 50]}
    exit_mode = "signal"

    def prepare(self, candles):
        closes = [c[4] for c in candles]
        self.fast = ema(closes, self.params["fast"])
        self.slow = ema(closes, self.params["slow"])

    def signal(self, i):
        if i < 1 or self.fast[i] is None or self.slow[i] is None:
            return "hold"
        if self.fast[i - 1] is None or self.slow[i - 1] is None:
            return "hold"
        crossed_up = self.fast[i - 1] <= self.slow[i - 1] and self.fast[i] > self.slow[i]
        crossed_down = self.fast[i - 1] >= self.slow[i - 1] and self.fast[i] < self.slow[i]
        if crossed_up:
            return "buy"
        if crossed_down:
            return "sell"
        return "hold"


# ---------------------------------------------------------------------------
# 2. RSI mean reversion — köp när något fallit "för mycket"
# ---------------------------------------------------------------------------

class RsiMeanReversion(Strategy):
    name = "rsi_mean_reversion"
    param_grid = {"period": [7, 14, 21], "oversold": [20, 25, 30], "overbought": [70, 75, 80]}
    exit_mode = "signal"

    def prepare(self, candles):
        closes = [c[4] for c in candles]
        self.rsi = rsi(closes, self.params["period"])

    def signal(self, i):
        v = self.rsi[i]
        if v is None:
            return "hold"
        if v <= self.params["oversold"]:
            return "buy"
        if v >= self.params["overbought"]:
            return "sell"
        return "hold"


# ---------------------------------------------------------------------------
# 3. MACD-crossover — momentum
# ---------------------------------------------------------------------------

class MacdCross(Strategy):
    name = "macd_cross"
    param_grid = {"fast": [8, 12], "slow": [21, 26], "signal_period": [7, 9]}
    exit_mode = "signal"

    def prepare(self, candles):
        closes = [c[4] for c in candles]
        self.macd_line, self.signal_line = macd(
            closes, self.params["fast"], self.params["slow"], self.params["signal_period"]
        )

    def signal(self, i):
        if i < 1:
            return "hold"
        m, s = self.macd_line[i], self.signal_line[i]
        pm, ps = self.macd_line[i - 1], self.signal_line[i - 1]
        if None in (m, s, pm, ps):
            return "hold"
        if pm <= ps and m > s:
            return "buy"
        if pm >= ps and m < s:
            return "sell"
        return "hold"


# ---------------------------------------------------------------------------
# 4. Bollinger breakout — köp när priset bryter ut ur sitt normala spann
# ---------------------------------------------------------------------------

class BollingerBreakout(Strategy):
    name = "bollinger_breakout"
    param_grid = {"period": [20, 30], "std_mult": [2.0, 2.5]}
    exit_mode = "rules"

    def prepare(self, candles):
        closes = [c[4] for c in candles]
        self.closes = closes
        self.mid = _sma(closes, self.params["period"])
        self.sd = _stddev(closes, self.params["period"])

    def signal(self, i):
        if self.mid[i] is None or self.sd[i] is None:
            return "hold"
        upper = self.mid[i] + self.params["std_mult"] * self.sd[i]
        if self.closes[i] > upper:
            return "buy"
        return "hold"


# ---------------------------------------------------------------------------
# 5. Bollinger reversion — motsatsen: köp vid nedre bandet
# ---------------------------------------------------------------------------

class BollingerReversion(Strategy):
    name = "bollinger_reversion"
    param_grid = {"period": [20, 30], "std_mult": [2.0, 2.5]}
    exit_mode = "signal"

    def prepare(self, candles):
        closes = [c[4] for c in candles]
        self.closes = closes
        self.mid = _sma(closes, self.params["period"])
        self.sd = _stddev(closes, self.params["period"])

    def signal(self, i):
        if self.mid[i] is None or self.sd[i] is None:
            return "hold"
        lower = self.mid[i] - self.params["std_mult"] * self.sd[i]
        if self.closes[i] < lower:
            return "buy"
        if self.closes[i] > self.mid[i]:
            return "sell"
        return "hold"


# ---------------------------------------------------------------------------
# 6. Donchian breakout — köp vid ny högsta nivå på N barer.
#    Detta är kärnan i den klassiska "Turtle Traders"-metoden.
# ---------------------------------------------------------------------------

class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    param_grid = {"period": [20, 40, 55]}
    exit_mode = "rules"

    def prepare(self, candles):
        self.closes = [c[4] for c in candles]
        highs = [c[2] for c in candles]
        p = self.params["period"]
        self.highest = []
        for i in range(len(highs)):
            if i < p:
                self.highest.append(None)
            else:
                self.highest.append(max(highs[i - p : i]))

    def signal(self, i):
        if self.highest[i] is None:
            return "hold"
        if self.closes[i] > self.highest[i]:
            return "buy"
        return "hold"


# ---------------------------------------------------------------------------
# 7. Volymspik-momentum — din nuvarande scanner-logik, i testbar form
# ---------------------------------------------------------------------------

class VolumeSpikeMomentum(Strategy):
    name = "volume_spike_momentum"
    param_grid = {"spike_mult": [2.0, 2.5, 3.5], "min_change_pct": [0.5, 1.0, 2.0]}
    exit_mode = "rules"

    def prepare(self, candles):
        self.closes = [c[4] for c in candles]
        volumes = [c[5] for c in candles]
        # Rullande medelvolym över 19 barer, exklusive den aktuella
        self.baseline = []
        for i in range(len(volumes)):
            if i < 20:
                self.baseline.append(None)
            else:
                self.baseline.append(sum(volumes[i - 20 : i]) / 20)
        self.volumes = volumes

    def signal(self, i):
        if i < 21 or self.baseline[i] is None or self.baseline[i] <= 0:
            return "hold"
        ratio = self.volumes[i] / self.baseline[i]
        change = (self.closes[i] - self.closes[i - 3]) / self.closes[i - 3] * 100
        if ratio >= self.params["spike_mult"] and change >= self.params["min_change_pct"]:
            # Redan rusat för mycket -> hoppa över
            if change > 15:
                return "hold"
            return "buy"
        return "hold"


# ---------------------------------------------------------------------------
# 8. Trendfilter + momentum — bara köp i medvind
#    Kombinerar två idéer: handla bara när den långa trenden är upp,
#    och gå in på momentum. Ett av de vanligaste sätten att förbättra
#    en momentumstrategi.
# ---------------------------------------------------------------------------

class TrendFilteredMomentum(Strategy):
    name = "trend_filtered_momentum"
    param_grid = {"trend_period": [50, 100], "spike_mult": [2.0, 3.0]}
    exit_mode = "rules"

    def prepare(self, candles):
        self.closes = [c[4] for c in candles]
        volumes = [c[5] for c in candles]
        self.trend = ema(self.closes, self.params["trend_period"])
        self.baseline = []
        for i in range(len(volumes)):
            if i < 20:
                self.baseline.append(None)
            else:
                self.baseline.append(sum(volumes[i - 20 : i]) / 20)
        self.volumes = volumes

    def signal(self, i):
        if i < 21 or self.trend[i] is None or self.baseline[i] is None or self.baseline[i] <= 0:
            return "hold"
        # Trendfilter: bara köp om priset ligger över den långa EMA:n
        if self.closes[i] <= self.trend[i]:
            return "hold"
        ratio = self.volumes[i] / self.baseline[i]
        change = (self.closes[i] - self.closes[i - 3]) / self.closes[i - 3] * 100
        if ratio >= self.params["spike_mult"] and 0.5 <= change <= 15:
            return "buy"
        return "hold"


# ---------------------------------------------------------------------------
# 9. Buy & hold — referensstrategin.
#    Slår en strategi inte denna är den inte värd sin komplexitet.
# ---------------------------------------------------------------------------

class BuyAndHold(Strategy):
    name = "buy_and_hold"
    param_grid = {}
    exit_mode = "signal"

    def prepare(self, candles):
        self.n = len(candles)

    def signal(self, i):
        return "buy" if i == 30 else "hold"


ALL_STRATEGIES = [
    EmaCross,
    RsiMeanReversion,
    MacdCross,
    BollingerBreakout,
    BollingerReversion,
    DonchianBreakout,
    VolumeSpikeMomentum,
    TrendFilteredMomentum,
    BuyAndHold,
]

STRATEGY_MAP = {s.name: s for s in ALL_STRATEGIES}


def expand_grid(strategy_cls) -> list[dict]:
    """Gör om param_grid till en lista av alla kombinationer."""
    grid = strategy_cls.param_grid
    if not grid:
        return [{}]

    keys = list(grid.keys())
    combos = [{}]
    for key in keys:
        new_combos = []
        for combo in combos:
            for value in grid[key]:
                c = dict(combo)
                c[key] = value
                new_combos.append(c)
        combos = new_combos

    # Filtrera bort ogiltiga kombinationer (t.ex. fast >= slow)
    valid = []
    for c in combos:
        if "fast" in c and "slow" in c and c["fast"] >= c["slow"]:
            continue
        if "oversold" in c and "overbought" in c and c["oversold"] >= c["overbought"]:
            continue
        valid.append(c)
    return valid
