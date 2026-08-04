"""
Strategy Lab — testar många strategier och parameterkombinationer
systematiskt, och skiljer riktig edge från tur.

DET HÄR ÄR MODULENS VIKTIGASTE IDÉ:

Testar du 500 strategikombinationer mot samma data kommer några se
lysande ut av ren slump. Med 500 tester förväntar man sig att ~25 stycken
råkar se "statistiskt signifikanta" ut vid 5%-nivån även om ingen av dem
har någon som helst edge. Att sedan välja den bästa och köra den live är
ett av de vanligaste sätten att förlora pengar på algoritmisk handel.

Skyddet: data delas i två delar.
    TRÄNING (första 65%)  — här söks alla kombinationer igenom
    TEST   (sista 35%)    — här utvärderas BARA vinnaren, en gång

Håller resultatet i testperioden är det troligen en riktig edge.
Kollapsar det, var det överanpassning — och du får veta det innan
pengarna är inblandade istället för efter.

EFFEKTIVITET:
- Candles hämtas EN gång per symbol och återanvänds för alla strategier
- Indikatorer räknas ut EN gång per kombination (inte per bar)
- Referensstrategin buy & hold körs alltid, som mätsticka
"""
import logging
from datetime import datetime, timezone

import strategies
import momentum_strategy
from backtest import FEE_PCT, SLIPPAGE_PCT

logger = logging.getLogger("lab")

TRAIN_FRACTION = 0.65
MIN_TRADES_FOR_CONFIDENCE = 30


def simulate(candles: list, strat, starting_balance: float = 1000.0) -> dict:
    """
    Kör en förberedd strategi genom en candle-serie och returnerar resultat.

    Ingen lookahead: signal(i) får bara se indikatorvärden beräknade från
    barer <= i, och affären genomförs till bar i:s stängningspris.
    """
    balance = starting_balance
    position = None
    trades = []
    equity = [starting_balance]

    for i in range(30, len(candles)):
        close = candles[i][4]
        ts = datetime.fromtimestamp(candles[i][0] / 1000, tz=timezone.utc)

        if position is None:
            if strat.signal(i) == "buy":
                position = {
                    "entry": close,
                    "amount": balance / close,
                    "entry_ts": ts,
                    "peak": close,
                }
        else:
            position["peak"] = max(position["peak"], close)
            exit_now, reason = False, ""

            if strat.exit_mode == "signal":
                if strat.signal(i) == "sell":
                    exit_now, reason = True, "SELL-signal"
            else:
                held_min = (ts - position["entry_ts"]).total_seconds() / 60
                exit_now, reason = momentum_strategy.check_exit(
                    {"avg_entry_price": position["entry"], "peak_price": position["peak"]},
                    close, held_min,
                )

            if exit_now:
                eff_entry = position["entry"] * (1 + SLIPPAGE_PCT / 100) * (1 + FEE_PCT / 100)
                eff_exit = close * (1 - SLIPPAGE_PCT / 100) * (1 - FEE_PCT / 100)
                cost = eff_entry * position["amount"]
                pnl = eff_exit * position["amount"] - cost
                balance += pnl
                equity.append(balance)
                trades.append({
                    "pnl": pnl,
                    "pnl_pct": pnl / cost * 100 if cost else 0,
                    "reason": reason,
                })
                position = None

    # Stäng eventuell öppen position på sista priset, annars blir
    # resultatet missvisande (en förlorande position som "aldrig stängdes"
    # skulle annars inte synas i statistiken alls).
    if position is not None:
        close = candles[-1][4]
        eff_entry = position["entry"] * (1 + SLIPPAGE_PCT / 100) * (1 + FEE_PCT / 100)
        eff_exit = close * (1 - SLIPPAGE_PCT / 100) * (1 - FEE_PCT / 100)
        cost = eff_entry * position["amount"]
        pnl = eff_exit * position["amount"] - cost
        balance += pnl
        equity.append(balance)
        trades.append({"pnl": pnl, "pnl_pct": pnl / cost * 100 if cost else 0,
                       "reason": "PERIOD SLUT"})

    return _metrics(trades, equity, starting_balance, balance)


def _metrics(trades, equity, starting_balance, ending_balance) -> dict:
    if not trades:
        return {
            "trades": 0, "return_pct": 0.0, "win_rate_pct": None,
            "profit_factor": None, "max_drawdown_pct": 0.0,
            "avg_win_pct": None, "avg_loss_pct": None, "exit_reasons": {},
        }

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))

    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        if peak:
            max_dd = max(max_dd, (peak - v) / peak * 100)

    reasons = {}
    for t in trades:
        k = t["reason"].split(":")[0]
        reasons[k] = reasons.get(k, 0) + 1

    return {
        "trades": len(trades),
        "return_pct": round((ending_balance - starting_balance) / starting_balance * 100, 2),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "avg_win_pct": round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "max_drawdown_pct": round(max_dd, 2),
        "exit_reasons": reasons,
    }


def run_lab(exchange, symbols: list[str], timeframe: str = "5m",
            limit: int = 1000) -> dict:
    """
    Kör ALLA strategier × alla parameterkombinationer × alla symboler,
    med tränings/testuppdelning.
    """
    # --- Hämta data en gång per symbol (den dyra delen) ---
    data = {}
    for sym in symbols:
        try:
            candles = exchange.fetch_ohlcv(sym, timeframe=timeframe, limit=limit)
            if len(candles) >= 200:
                data[sym] = candles
                logger.info("Hämtade %d candles för %s", len(candles), sym)
        except Exception as e:
            logger.error("Kunde inte hämta %s: %s", sym, e)

    if not data:
        return {"error": "Ingen data kunde hämtas"}

    combos_tested = 0
    results = []

    skipped = []
    for strat_cls in strategies.ALL_STRATEGIES:
        # Order flow-strategier kan inte backtestas: orderboksdjup och
        # affärsriktning sparas bara framåt i tiden, inte historiskt.
        # De utvärderas istället i bot-arenan (forward testing).
        if strat_cls.needs_context:
            skipped.append(strat_cls.name)
            continue
        for params in strategies.expand_grid(strat_cls):
            combos_tested += 1
            train_returns, test_returns = [], []
            train_trades, test_trades = 0, 0
            per_symbol = []

            for sym, candles in data.items():
                split = int(len(candles) * TRAIN_FRACTION)
                train_candles = candles[:split]
                test_candles = candles[split:]

                if len(train_candles) < 100 or len(test_candles) < 60:
                    continue

                strat_train = strat_cls(**params)
                strat_train.prepare(train_candles)
                r_train = simulate(train_candles, strat_train)

                strat_test = strat_cls(**params)
                strat_test.prepare(test_candles)
                r_test = simulate(test_candles, strat_test)

                train_returns.append(r_train["return_pct"])
                test_returns.append(r_test["return_pct"])
                train_trades += r_train["trades"]
                test_trades += r_test["trades"]

                per_symbol.append({
                    "symbol": sym,
                    "train_return_pct": r_train["return_pct"],
                    "test_return_pct": r_test["return_pct"],
                    "test_trades": r_test["trades"],
                    "test_win_rate_pct": r_test["win_rate_pct"],
                    "test_profit_factor": r_test["profit_factor"],
                    "test_max_drawdown_pct": r_test["max_drawdown_pct"],
                    "test_exit_reasons": r_test["exit_reasons"],
                })

            if not per_symbol:
                continue

            avg_train = sum(train_returns) / len(train_returns)
            avg_test = sum(test_returns) / len(test_returns)
            profitable_symbols = len([r for r in test_returns if r > 0])

            results.append({
                "strategy": strat_cls.name,
                "params": params,
                "label": strat_cls(**params).describe(),
                "avg_train_return_pct": round(avg_train, 2),
                "avg_test_return_pct": round(avg_test, 2),
                "train_trades": train_trades,
                "test_trades": test_trades,
                "profitable_symbols": profitable_symbols,
                "symbols_tested": len(per_symbol),
                "consistency_pct": round(profitable_symbols / len(per_symbol) * 100, 1),
                # Skillnaden mellan träning och test avslöjar överanpassning:
                # stor positiv siffra = såg bra ut på gammal data, funkar inte på ny
                "overfit_gap": round(avg_train - avg_test, 2),
                "per_symbol": per_symbol,
            })

    # Sortera på TESTresultat, inte träningsresultat — det är hela poängen
    results.sort(key=lambda r: r["avg_test_return_pct"], reverse=True)

    baseline = next((r for r in results if r["strategy"] == "buy_and_hold"), None)
    baseline_return = baseline["avg_test_return_pct"] if baseline else 0.0

    return {
        "meta": {
            "symbols": list(data.keys()),
            "timeframe": timeframe,
            "candles_per_symbol": limit,
            "combinations_tested": combos_tested,
            "train_fraction": TRAIN_FRACTION,
            "fee_pct_per_side": FEE_PCT,
            "slippage_pct_per_side": SLIPPAGE_PCT,
        },
        "baseline_buy_and_hold_pct": baseline_return,
        "skipped_strategies": {
            "names": skipped,
            "reason": "Order flow-strategier kan inte backtestas — orderboksdata "
                      "finns bara framåt i tiden. Se bot-arenan istället.",
        },
        "top_results": results[:15],
        "verdict": _verdict(results, baseline_return, combos_tested),
    }


def _verdict(results: list[dict], baseline: float, combos: int) -> dict:
    """En ärlig bedömning, inte bara en topplista."""
    if not results:
        return {"summary": "Inga resultat kunde beräknas."}

    real = [r for r in results if r["strategy"] != "buy_and_hold"]
    if not real:
        return {"summary": "Bara referensstrategin kunde köras."}

    best = real[0]
    warnings = []

    if best["test_trades"] < MIN_TRADES_FOR_CONFIDENCE:
        warnings.append(
            f"Vinnaren gjorde bara {best['test_trades']} affärer i testperioden. "
            f"Under {MIN_TRADES_FOR_CONFIDENCE} är resultatet mest slump."
        )

    if best["overfit_gap"] > 10:
        warnings.append(
            f"Stor skillnad mellan träning ({best['avg_train_return_pct']}%) och "
            f"test ({best['avg_test_return_pct']}%) — tecken på överanpassning."
        )

    if best["consistency_pct"] < 60:
        warnings.append(
            f"Lönsam på bara {best['consistency_pct']}% av symbolerna. "
            "En riktig edge brukar fungera på flera marknader."
        )

    if best["avg_test_return_pct"] <= baseline:
        warnings.append(
            f"Slår inte buy & hold ({baseline}%). Då är strategin inte värd "
            "sin komplexitet och risk."
        )

    # Ju fler kombinationer som testats, desto större chans att toppen är tur
    warnings.append(
        f"{combos} kombinationer testades. Med så många tester förväntas några "
        "se bra ut av ren slump — det är därför testperioden finns."
    )

    if best["avg_test_return_pct"] <= 0:
        summary = (
            "Ingen strategi var lönsam på testdata. Det vanligaste och ärligaste "
            "utfallet — enkla tekniska strategier har sällan en edge efter avgifter."
        )
    elif len(warnings) > 2:
        summary = (
            f"Bästa: {best['label']} (+{best['avg_test_return_pct']}% på testdata), "
            "men med flera varningsflaggor. Behandla som osäkert."
        )
    else:
        summary = (
            f"Bästa: {best['label']} (+{best['avg_test_return_pct']}% på testdata) "
            f"med {best['consistency_pct']}% konsistens. Värt att köra vidare på papper — "
            "men bekräfta i en annan tidsperiod innan riktiga pengar."
        )

    return {
        "summary": summary,
        "best_strategy": best["label"],
        "best_test_return_pct": best["avg_test_return_pct"],
        "beats_buy_and_hold": best["avg_test_return_pct"] > baseline,
        "warnings": warnings,
    }
