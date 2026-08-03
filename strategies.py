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
    # True = strategin behöver order flow-data (kan inte backtestas, se lab.py)
    needs_context = False

    def __init__(self, **params):
        self.params = params
        self.context = None

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
# 9. Order flow — följer vad köpare och säljare faktiskt gör
#
#    Detta är en fundamentalt annan sorts signal än de andra. EMA och RSI
#    räknar på priset, som är RESULTATET av handeln. Order flow tittar på
#    orsaken: vem som köper, hur mycket, och hur aggressivt.
#
#    KAN INTE BACKTESTAS: orderboksdjup och affärsriktning sparas bara
#    framåt i tiden, inte historiskt. Arenan (forward testing) är därför
#    enda ärliga sättet att utvärdera den — vilket också gör den till ett
#    rent test av om order flow slår teknisk analys.
# ---------------------------------------------------------------------------

class OrderFlowPressure(Strategy):
    name = "order_flow_pressure"
    param_grid = {"entry_score": [0.35, 0.5], "exit_score": [-0.2, -0.35]}
    exit_mode = "signal"
    needs_context = True

    def prepare(self, candles):
        self.closes = [c[4] for c in candles]

    def signal(self, i):
        if not self.context or not self.context.get("available"):
            return "hold"
        score = self.context["score"]
        if score >= self.params["entry_score"]:
            return "buy"
        if score <= self.params["exit_score"]:
            return "sell"
        return "hold"


class WhaleFollow(Strategy):
    """
    Handlar bara på stora affärer och absorption — ignorerar allt annat.

    Idén: en affär 8x större än medianen kommer inte från en privatperson.
    Absorption (hög volym, priset står still) är hur stora aktörer bygger
    positioner utan att jaga upp priset mot sig själva.
    """
    name = "whale_follow"
    param_grid = {"min_whale_net_ratio": [0.15, 0.30]}
    exit_mode = "rules"
    needs_context = True

    def prepare(self, candles):
        self.closes = [c[4] for c in candles]

    def signal(self, i):
        if not self.context or not self.context.get("available"):
            return "hold"

        ctx = self.context
        total = ctx.get("total_volume") or 0
        if total <= 0:
            return "hold"

        whale_ratio = (ctx.get("whale_net_volume") or 0) / total

        # Absorption på köpsidan är den starkaste enskilda signalen
        if ctx.get("absorption") and ctx.get("absorption_side") == "buy":
            return "buy"

        if whale_ratio >= self.params["min_whale_net_ratio"] and ctx.get("whale_buys", 0) >= 2:
            return "buy"

        return "hold"


# ---------------------------------------------------------------------------
# 11. AI Decision Engine — väger samman ALLA signalkällor
#     Detta är Fas 8: teknisk analys + order flow + social hype + regim,
#     med vikter satta efter hur pålitlig varje källa är i princip.
# ---------------------------------------------------------------------------

class EnsembleDecision(Strategy):
    name = "ensemble_ai"
    param_grid = {}
    exit_mode = "signal"
    needs_context = True

    def prepare(self, candles):
        self.candles = candles

    def signal(self, i):
        if not self.context:
            return "hold"
        import decision_engine
        result = decision_engine.decide(
            self.context.get("symbol", ""),
            self.candles,
            flow=self.context.get("flow"),
            hype=self.context.get("hype"),
            regime_data=self.context.get("regime"),
        )
        self.last_decision = result
        return result["decision"]


# ---------------------------------------------------------------------------
# 12. Buy & hold — referensstrategin.
#    Slår en strategi inte denna är den inte värd sin komplexitet.
# ---------------------------------------------------------------------------

class BuyAndHold(Strategy):
    name = "buy_and_hold"
    param_grid = {}
    exit_mode = "signal"

    def prepare(self, candles):
        self.n = len(candles)

    def signal(self, i):
        # Alltid "buy": den som anropar köper bara om ingen position finns,
        # och eftersom vi aldrig returnerar "sell" hålls den för alltid.
        #
        # Tidigare stod här `i == 30`, vilket fungerade i backtesten (som
        # loopar igenom varje bar) men ALDRIG i bot-arenan (som bara tittar
        # på senaste baren, index ~199). Referensboten köpte alltså aldrig
        # något, och hela jämförelsegrunden var trasig.
        return "buy"


ALL_STRATEGIES = [
    EmaCross,
    RsiMeanReversion,
    MacdCross,
    BollingerBreakout,
    BollingerReversion,
    DonchianBreakout,
    VolumeSpikeMomentum,
    TrendFilteredMomentum,
    OrderFlowPressure,
    WhaleFollow,
    EnsembleDecision,
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
