"""
Backtesting Engine (Fas 5).

Kör strategierna mot historisk data för att se hur de HADE presterat.

Två saker gör skillnad mellan en backtest som säger sanningen och en som
lurar dig, och båda är hanterade här:

1. LOOKAHEAD BIAS — att av misstag använda data från framtiden.
   Här löst genom att loopen bara får se candles[:i+1] vid varje steg.
   Strategin kan alltså aldrig "se" priset den ska handla på.

2. AVGIFTER OCH SLIPPAGE — den vanligaste anledningen till att en
   "lönsam" backtest blir en förlust i verkligheten. En strategi med
   +0,3% snittvinst per affär är i själva verket en förlustaffär när
   0,1% avgift åt varje håll och slippage dras av.

Backtesten använder SAMMA strategikod som liveboten (strategy.py,
momentum_strategy.py, scanner-logiken). Bygger man en separat
"backtest-version" av strategin testar man i praktiken något annat än
det som faktiskt körs.
"""
import logging
from datetime import datetime, timezone

import strategy
import momentum_strategy
import technical
from config import settings

logger = logging.getLogger("backtest")

# --- Realistiska antaganden -------------------------------------------------
# KuCoin spot taker-avgift är ca 0,1% per affär (både köp och sälj).
FEE_PCT = 0.1
# Slippage: skillnaden mellan priset du ser och priset du får.
# 0,15% är en försiktig gissning för mindre likvida par. På riktigt
# illikvida tokens kan det vara flera procent.
SLIPPAGE_PCT = 0.15


class BacktestResult:
    """Samlar resultat och räknar ut nyckeltal."""

    def __init__(self, symbol: str, strategy_name: str, starting_balance: float):
        self.symbol = symbol
        self.strategy_name = strategy_name
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.trades: list[dict] = []
        self.equity_curve: list[float] = [starting_balance]

    def record_trade(self, entry_price, exit_price, amount, entry_ts, exit_ts, reason):
        # Avgift och slippage dras av åt båda hållen
        effective_entry = entry_price * (1 + SLIPPAGE_PCT / 100) * (1 + FEE_PCT / 100)
        effective_exit = exit_price * (1 - SLIPPAGE_PCT / 100) * (1 - FEE_PCT / 100)

        cost = effective_entry * amount
        proceeds = effective_exit * amount
        pnl = proceeds - cost
        pnl_pct = pnl / cost * 100 if cost else 0

        self.balance += pnl
        self.equity_curve.append(self.balance)

        self.trades.append({
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "amount": amount,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "held_minutes": (exit_ts - entry_ts).total_seconds() / 60,
        })

    def metrics(self) -> dict:
        if not self.trades:
            return {
                "symbol": self.symbol,
                "strategy": self.strategy_name,
                "trades": 0,
                "note": "Inga affärer genomfördes under perioden — "
                        "strategin hittade aldrig en signal som passerade tröskeln.",
                "starting_balance": self.starting_balance,
                "ending_balance": self.balance,
                "total_return_pct": 0.0,
            }

        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]

        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))

        # Max drawdown — största fallet från en topp i kapitalkurvan.
        # Detta är ofta viktigare än totalavkastningen: en strategi som
        # tjänar 40% men tappar 30% på vägen är svår att faktiskt köra.
        peak = self.equity_curve[0]
        max_dd = 0.0
        for value in self.equity_curve:
            peak = max(peak, value)
            dd = (peak - value) / peak * 100 if peak else 0
            max_dd = max(max_dd, dd)

        # Exit-anledningar — visar VARFÖR affärerna stängdes
        exit_reasons = {}
        for t in self.trades:
            key = t["reason"].split(":")[0]
            exit_reasons[key] = exit_reasons.get(key, 0) + 1

        return {
            "symbol": self.symbol,
            "strategy": self.strategy_name,
            "starting_balance": self.starting_balance,
            "ending_balance": round(self.balance, 2),
            "total_return_pct": round((self.balance - self.starting_balance) / self.starting_balance * 100, 2),
            "trades": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(self.trades) * 100, 1),
            "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0,
            "best_trade_pct": round(max(t["pnl_pct"] for t in self.trades), 2),
            "worst_trade_pct": round(min(t["pnl_pct"] for t in self.trades), 2),
            # Profit factor: bruttovinst / bruttoförlust. Under 1.0 = förlustsystem.
            # Över 1.5 brukar anses användbart, men se varningen nedan.
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
            "max_drawdown_pct": round(max_dd, 2),
            "avg_hold_minutes": round(sum(t["held_minutes"] for t in self.trades) / len(self.trades), 1),
            "exit_reasons": exit_reasons,
            "assumptions": {
                "fee_pct_per_side": FEE_PCT,
                "slippage_pct_per_side": SLIPPAGE_PCT,
            },
        }


def fetch_history(exchange, symbol: str, timeframe: str = "5m", limit: int = 1000) -> list:
    """
    Hämtar historiska candles från KuCoin.
    ccxt begränsar oftast till ~1500 per anrop, så vi håller oss under det.
    """
    candles = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    logger.info("Hämtade %d candles för %s (%s)", len(candles), symbol, timeframe)
    return candles


def backtest_momentum(candles: list, starting_balance: float = 1000.0,
                       position_size_pct: float = 1.0) -> BacktestResult:
    """
    Backtestar momentum-strategin på EN symbols historik.

    Simulerar scannerns entry-logik (volymspik + tidig prisrörelse) och
    momentum_strategy:s exit-regler, bar för bar.

    OBS: en riktig scan väljer BÄST av hundratals tokens vid varje
    tillfälle. Detta testar bara "hade strategin tjänat pengar på just
    den här token" — vilket är en strängare och ärligare fråga än att
    i efterhand plocka ut vinnarna.
    """
    result = BacktestResult(candles and "symbol" or "", "momentum", starting_balance)

    position = None  # {entry_price, amount, entry_ts, peak_price, partial_taken, stop_pct}

    # Börja vid index 30 så det finns historik att räkna volymsnitt på
    for i in range(30, len(candles)):
        # KRITISKT: bara data fram till och med i får användas.
        window = candles[: i + 1]
        current = candles[i]
        ts = datetime.fromtimestamp(current[0] / 1000, tz=timezone.utc)
        close = current[4]

        if position is None:
            # --- Entry-logik (samma som scanner.analyze_candidate) ---
            volumes = [c[5] for c in window[-30:]]
            closes = [c[4] for c in window[-30:]]

            recent_volume = volumes[-1]
            baseline = sum(volumes[-20:-1]) / 19
            if baseline <= 0:
                continue
            volume_ratio = recent_volume / baseline
            price_change_pct = (closes[-1] - closes[-4]) / closes[-4] * 100
            prev_change_pct = (closes[-4] - closes[-7]) / closes[-7] * 100
            accelerating = price_change_pct > prev_change_pct

            import scanner
            score = 0.0
            if volume_ratio >= scanner.VOLUME_SPIKE_MIN:
                score += min(volume_ratio / scanner.VOLUME_SPIKE_MIN, 3.0)
            if scanner.PRICE_CHANGE_MIN_PCT <= price_change_pct <= scanner.PRICE_CHANGE_MAX_PCT:
                score += 1.0
            elif price_change_pct > scanner.PRICE_CHANGE_MAX_PCT:
                score -= 2.0
            if accelerating:
                score += 0.5

            if score >= momentum_strategy.MIN_ENTRY_SCORE:
                spend = result.balance * position_size_pct
                atr_pct = technical.atr(window[-20:])
                stop_pct = momentum_strategy.STOP_LOSS_PCT
                if atr_pct:
                    stop_pct = max(min(-(atr_pct * 1.5), momentum_strategy.STOP_LOSS_PCT),
                                   -abs(settings.max_stop_loss_pct))
                position = {
                    "entry_price": close,
                    "amount": spend / close,
                    "entry_ts": ts,
                    "peak_price": close,
                    "stop_pct": stop_pct,
                }
        else:
            # --- Exit-logik (samma funktioner som liveboten) ---
            position["peak_price"] = max(position["peak_price"], close)
            held_minutes = (ts - position["entry_ts"]).total_seconds() / 60

            pos_dict = {
                "avg_entry_price": position["entry_price"],
                "peak_price": position["peak_price"],
            }
            should_exit, reason = momentum_strategy.check_exit(
                pos_dict, close, held_minutes, stop_loss_pct=position["stop_pct"]
            )

            if should_exit:
                result.record_trade(
                    position["entry_price"], close, position["amount"],
                    position["entry_ts"], ts, reason,
                )
                position = None

    return result


def backtest_technical(candles: list, starting_balance: float = 1000.0,
                        position_size_pct: float = 1.0) -> BacktestResult:
    """
    Backtestar den lugna EMA/RSI/MACD-strategin (strategy.py).
    Köper på 'buy'-signal, säljer på 'sell'-signal.
    """
    result = BacktestResult("", "technical", starting_balance)
    position = None

    for i in range(30, len(candles)):
        window = candles[: i + 1]
        current = candles[i]
        ts = datetime.fromtimestamp(current[0] / 1000, tz=timezone.utc)
        close = current[4]

        # strategy.decide förväntar sig dicts med "close"
        candle_dicts = [{"close": c[4]} for c in window[-100:]]
        decision = strategy.decide(candle_dicts)

        if position is None and decision["decision"] == "buy":
            spend = result.balance * position_size_pct
            position = {"entry_price": close, "amount": spend / close, "entry_ts": ts}
        elif position is not None and decision["decision"] == "sell":
            result.record_trade(
                position["entry_price"], close, position["amount"],
                position["entry_ts"], ts, f"SELL-signal (score {decision['score']})",
            )
            position = None

    return result


def run(exchange, symbol: str, strategy_name: str = "momentum",
        timeframe: str = "5m", limit: int = 1000,
        starting_balance: float = 1000.0) -> dict:
    """
    Kör en komplett backtest och returnerar nyckeltal + affärslista.
    """
    candles = fetch_history(exchange, symbol, timeframe, limit)

    if len(candles) < 50:
        return {"error": f"För lite historik för {symbol} ({len(candles)} candles)"}

    if strategy_name == "momentum":
        result = backtest_momentum(candles, starting_balance)
    elif strategy_name == "technical":
        result = backtest_technical(candles, starting_balance)
    else:
        return {"error": f"Okänd strategi: {strategy_name}"}

    result.symbol = symbol
    metrics = result.metrics()

    period_start = datetime.fromtimestamp(candles[0][0] / 1000, tz=timezone.utc)
    period_end = datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc)

    # Buy & hold som jämförelse — slår strategin ens att bara köpa och vänta?
    # Detta är den jämförelse som oftast avslöjar att en strategi inte är
    # värd komplexiteten.
    buy_hold_pct = (candles[-1][4] - candles[0][4]) / candles[0][4] * 100

    metrics.update({
        "timeframe": timeframe,
        "candles": len(candles),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "buy_and_hold_pct": round(buy_hold_pct, 2),
        "beat_buy_and_hold": metrics["total_return_pct"] > buy_hold_pct,
    })

    return {"metrics": metrics, "trades": result.trades[-50:]}
