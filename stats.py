"""
Statistisk prövning — är resultatet en kant eller ett lyckat urval?

FRÅGAN SOM SKA BESVARAS: `bollinger_reversion` gav +0,53% per affär över
358 affärer. Är det en verklig kant, eller ett urval som råkade falla rätt?

Medelvärdet ensamt kan inte svara på det. En strategi där två affärer av
hundra ger +50% och resten −0,5% har också ett positivt medelvärde — men
den är ett lotteri, inte en kant.

FYRA TESTER, som svarar på olika saker:

1. BOOTSTRAP
   Dra 10 000 slumpmässiga urval MED återläggning ur de faktiska
   affärerna. Om strategin har en kant blir de flesta urvalen positiva.
   Om resultatet bärs av några få träffar kommer många urval som missar
   dem att bli negativa.

   Det här är kärnan: vi frågar "hur hade det gått om historien spelats
   om?" utan att anta någon fördelning.

2. KONCENTRATION
   Hur mycket av vinsten kommer från de bästa affärerna? Tas de fem
   bästa bort — vad återstår? En robust strategi tål det. Ett lotteri
   kollapsar.

3. TIDSSTABILITET
   Dela perioden i två halvor. Var strategin lönsam i båda? En kant som
   bara finns i andra halvan är troligen ett marknadsläge, inte en kant.

4. MULTIPELTESTNING
   Med elva strategier testade förväntas några se bra ut av ren slump.
   Vi räknar ut hur sannolikt det är att MINST en av elva ser så här bra
   ut även om ingen har en kant.

   Detta är det viktigaste testet och det som oftast glöms bort.
"""
import logging
import random
import statistics

from db import get_cursor

logger = logging.getLogger("stats")

BOOTSTRAP_SAMPLES = 10_000


def _get_paired_trades(bot_id: int) -> list[dict]:
    """Parar ihop köp med sälj och returnerar avkastning per affär."""
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT symbol, side, price, quote_amount, realized_pnl, ts "
            "FROM bot_trades WHERE bot_id = %s ORDER BY ts",
            (bot_id,),
        )
        rows = cur.fetchall()

    open_buys = {}
    trades = []
    for symbol, side, price, quote_amount, pnl, ts in rows:
        if side == "buy":
            open_buys[symbol] = {"price": float(price), "quote": float(quote_amount), "ts": ts}
        elif side == "sell" and symbol in open_buys:
            buy = open_buys.pop(symbol)
            net_pnl = float(pnl) if pnl is not None else 0.0
            trades.append({
                "symbol": symbol,
                "net_pnl": net_pnl,
                "net_pct": net_pnl / buy["quote"] * 100 if buy["quote"] else 0.0,
                "gross_pct": (float(price) - buy["price"]) / buy["price"] * 100,
                "ts": ts,
            })
    return trades


def bootstrap(returns: list[float], samples: int = BOOTSTRAP_SAMPLES) -> dict:
    """
    Drar slumpmässiga urval med återläggning och mäter hur ofta resultatet
    blir positivt.

    Tolkning av `positive_rate`:
      över 95%  — resultatet håller i nästan alla omspelningar
      90-95%    — lovande men inte avgjort
      under 90% — resultatet beror på vilka affärer som råkade ingå
    """
    n = len(returns)
    if n < 20:
        return {"error": f"För få affärer ({n}) för bootstrap"}

    rng = random.Random(42)  # fast frö så resultatet går att reproducera
    means = []
    for _ in range(samples):
        sample = [returns[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)

    means.sort()
    positive = len([m for m in means if m > 0])

    def pct(p):
        return means[min(int(p / 100 * samples), samples - 1)]

    return {
        "samples": samples,
        "observed_mean_pct": round(sum(returns) / n, 4),
        "bootstrap_mean_pct": round(sum(means) / samples, 4),
        "positive_rate_pct": round(positive / samples * 100, 2),
        "ci_5_pct": round(pct(5), 4),
        "ci_50_pct": round(pct(50), 4),
        "ci_95_pct": round(pct(95), 4),
        "worst_case_pct": round(means[0], 4),
        "best_case_pct": round(means[-1], 4),
    }


def concentration(trades: list[dict]) -> dict:
    """
    Hur beroende är resultatet av de bästa affärerna?

    En strategi vars vinst försvinner när fem affärer tas bort har inte
    en kant — den hade tur några gånger.
    """
    if len(trades) < 20:
        return {"error": "För få affärer"}

    pnls = sorted([t["net_pnl"] for t in trades], reverse=True)
    total = sum(pnls)

    def without_top(k):
        rest = pnls[k:]
        return {
            "removed": k,
            "remaining_pnl": round(sum(rest), 2),
            "still_profitable": sum(rest) > 0,
        }

    best = pnls[0]
    top5 = sum(pnls[:5])

    return {
        "total_pnl": round(total, 2),
        "best_trade_pnl": round(best, 2),
        "best_trade_share_pct": round(best / total * 100, 1) if total > 0 else None,
        "top5_share_pct": round(top5 / total * 100, 1) if total > 0 else None,
        "without_top_1": without_top(1),
        "without_top_3": without_top(3),
        "without_top_5": without_top(5),
        "without_top_10": without_top(10),
        "median_trade_pnl": round(statistics.median(pnls), 3),
        "mean_trade_pnl": round(total / len(pnls), 3),
    }


def time_stability(trades: list[dict]) -> dict:
    """
    Var strategin lönsam i båda halvorna av perioden?

    En kant som bara finns i andra halvan är troligen ett marknadsläge
    som råkade passa strategin, inte en egenskap hos strategin.
    """
    if len(trades) < 40:
        return {"error": "För få affärer för att dela perioden"}

    ordered = sorted(trades, key=lambda t: t["ts"])
    mid = len(ordered) // 2
    first, second = ordered[:mid], ordered[mid:]

    def stats(chunk):
        pnls = [t["net_pnl"] for t in chunk]
        pcts = [t["net_pct"] for t in chunk]
        wins = len([p for p in pnls if p > 0])
        return {
            "trades": len(chunk),
            "total_pnl": round(sum(pnls), 2),
            "avg_pct": round(sum(pcts) / len(pcts), 4),
            "win_rate_pct": round(wins / len(chunk) * 100, 1),
            "profitable": sum(pnls) > 0,
        }

    f, s = stats(first), stats(second)
    return {
        "first_half": f,
        "second_half": s,
        "consistent": f["profitable"] and s["profitable"],
        "note": (
            "Lönsam i båda halvorna — det talar för att kanten är stabil."
            if f["profitable"] and s["profitable"] else
            "Lönsam i bara en halva. Resultatet kan bero på marknadsläget "
            "under just den perioden snarare än på strategin."
        ),
    }


def multiple_testing_adjustment(n_strategies: int, best_positive_rate: float) -> dict:
    """
    Med elva strategier testade förväntas några se bra ut av ren slump.

    Om en enskild strategi utan kant har säg 5% chans att se lönsam ut,
    är chansen att MINST en av elva gör det inte 5% utan
    1 - (0.95)^11 = 43%.

    Det är därför "bäst av elva" är ett mycket svagare bevis än "den enda
    vi testade".
    """
    p_single = 1 - (best_positive_rate / 100)
    p_at_least_one = 1 - (1 - p_single) ** n_strategies

    return {
        "strategies_tested": n_strategies,
        "single_strategy_false_positive_pct": round(p_single * 100, 2),
        "at_least_one_false_positive_pct": round(p_at_least_one * 100, 2),
        "note": (
            f"Med {n_strategies} testade strategier är sannolikheten att minst "
            f"en ser så här bra ut utan att ha en kant {p_at_least_one*100:.1f}%. "
            "Ju fler strategier som testats, desto svagare är beviset för den "
            "som råkade bli bäst."
        ),
    }


def analyze_bot(bot_id: int, bot_name: str, n_strategies_tested: int = 11) -> dict:
    trades = _get_paired_trades(bot_id)
    if len(trades) < 20:
        return {
            "bot": bot_name,
            "trades": len(trades),
            "verdict": f"För få affärer ({len(trades)}) för statistisk prövning.",
        }

    returns = [t["net_pct"] for t in trades]
    boot = bootstrap(returns)
    conc = concentration(trades)
    stab = time_stability(trades)
    mult = multiple_testing_adjustment(n_strategies_tested,
                                       boot.get("positive_rate_pct", 50))

    return {
        "bot": bot_name,
        "trades": len(trades),
        "bootstrap": boot,
        "concentration": conc,
        "time_stability": stab,
        "multiple_testing": mult,
        "verdict": _verdict(boot, conc, stab, mult),
    }


def _verdict(boot, conc, stab, mult) -> str:
    if "error" in boot:
        return boot["error"]

    parts = []
    pos = boot["positive_rate_pct"]
    flags = 0

    if pos >= 95:
        parts.append(f"Bootstrap: {pos}% av 10 000 omspelningar gav positivt resultat.")
    elif pos >= 90:
        parts.append(f"Bootstrap: {pos}% positiva — lovande men inte avgjort.")
        flags += 1
    else:
        parts.append(
            f"Bootstrap: bara {pos}% av omspelningarna gav positivt resultat. "
            "Utfallet beror i hög grad på vilka affärer som råkade ingå."
        )
        flags += 2

    if "error" not in conc:
        top5 = conc.get("top5_share_pct")
        if top5 and top5 > 100:
            parts.append(
                f"De fem bästa affärerna står för {top5:.0f}% av vinsten — "
                "utan dem är strategin i förlust."
            )
            flags += 2
        elif top5 and top5 > 60:
            parts.append(f"De fem bästa affärerna står för {top5:.0f}% av vinsten.")
            flags += 1
        if not conc["without_top_5"]["still_profitable"]:
            parts.append("Tas de fem bästa affärerna bort blir resultatet negativt.")
            flags += 1
        if conc.get("median_trade_pnl", 0) < 0:
            parts.append(
                "Medianaffären är negativ — en TYPISK affär förlorar pengar, "
                "medelvärdet räddas av några få stora vinnare."
            )
            flags += 2

    if "error" not in stab:
        if stab["consistent"]:
            parts.append("Lönsam i båda halvorna av perioden.")
        else:
            parts.append("Lönsam i bara en av periodens två halvor.")
            flags += 1

    parts.append(mult["note"])

    if flags == 0:
        head = "HÅLLER: resultatet överlever alla tester."
    elif flags <= 2:
        head = "LOVANDE MEN OSÄKERT: några varningsflaggor."
    else:
        head = "HÅLLER INTE: resultatet bygger sannolikt på tur."

    return f"{head} " + " ".join(parts)


def analyze_all() -> dict:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, name FROM bots ORDER BY id")
        bots = cur.fetchall()

    results = []
    for bot_id, name in bots:
        try:
            results.append(analyze_bot(bot_id, name, len(bots)))
        except Exception as e:
            logger.error("Statistisk prövning misslyckades för %s: %s", name, e)

    tested = [r for r in results if "bootstrap" in r]
    holds = [r for r in tested if r["verdict"].startswith("HÅLLER:")]
    uncertain = [r for r in tested if r["verdict"].startswith("LOVANDE")]

    return {
        "bots": results,
        "summary": {
            "bots_tested": len(tested),
            "survived_all_tests": [r["bot"] for r in holds],
            "promising_but_uncertain": [r["bot"] for r in uncertain],
            "conclusion": (
                f"Ingen av {len(tested)} strategier överlevde den statistiska "
                "prövningen. Det vanligaste utfallet — och det tydligaste svaret "
                "du kan få."
                if not holds and not uncertain else
                f"Överlevde alla tester: {', '.join(r['bot'] for r in holds) or 'ingen'}. "
                f"Lovande men osäkra: {', '.join(r['bot'] for r in uncertain) or 'inga'}. "
                "Kom ihåg multipeltestningen: med elva strategier förväntas några "
                "se bra ut även utan kant."
            ),
        },
    }
