"""
Botanalys — skiljer strategifel från friktionsfel.

DET AVGÖRANDE MÅTTET som saknades i topplistan:

    BRUTTO  = hur mycket priset rörde sig mellan köp och sälj
    NETTO   = vad boten faktiskt fick behålla efter avgift och slippage

Skillnaden mellan dem är friktionen. Och den skiljer två helt olika
problem åt:

    Brutto positivt, netto negativt
        -> Strategin gissar rätt riktning men handlar för ofta.
           Lösningen är färre affärer, inte en annan strategi.

    Brutto negativt
        -> Strategin gissar fel riktning. Ingen mängd
           friktionsoptimering hjälper.

Utan denna uppdelning ser båda fallen identiska ut i topplistan: en bot
som förlorar pengar. Men det ena är ett justerbart problem och det andra
är ett dött spår.

HUR BRUTTO RÄKNAS FRAM: bot_trades sparar RÅPRISET för varje köp och sälj.
Genom att para ihop dem i tidsordning per bot och symbol får vi den rena
prisrörelsen, utan avgifter. realized_pnl innehåller redan avgifter och
slippage, alltså nettot.
"""
import logging
import statistics

from db import get_cursor

logger = logging.getLogger("analysis")

# Affärer kortare än så här räknas som "flip" — köpt och sålt nästan direkt.
# Sådana kan nästan aldrig täcka sina egna kostnader.
FLIP_MINUTES = 15


def analyze_bot(bot_id: int, bot_name: str, strategy: str) -> dict:
    """Analyserar en bots affärshistorik och parar ihop köp med sälj."""
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT symbol, side, price, amount, quote_amount, realized_pnl, "
            "fees_paid, reason, ts FROM bot_trades WHERE bot_id = %s ORDER BY ts",
            (bot_id,),
        )
        rows = cur.fetchall()

    # Para ihop köp -> sälj per symbol
    open_buys = {}
    pairs = []
    total_fees = 0.0

    for symbol, side, price, amount, quote_amount, pnl, fees, reason, ts in rows:
        total_fees += float(fees or 0)
        if side == "buy":
            open_buys[symbol] = {"price": float(price), "ts": ts, "quote": float(quote_amount)}
        elif side == "sell" and symbol in open_buys:
            buy = open_buys.pop(symbol)
            gross_pct = (float(price) - buy["price"]) / buy["price"] * 100
            net_pnl = float(pnl) if pnl is not None else 0.0
            net_pct = net_pnl / buy["quote"] * 100 if buy["quote"] else 0.0
            pairs.append({
                "symbol": symbol,
                "gross_pct": gross_pct,
                "net_pnl": net_pnl,
                "net_pct": net_pct,
                "hold_minutes": (ts - buy["ts"]).total_seconds() / 60,
                "reason": reason or "",
            })

    if not pairs:
        return {
            "bot": bot_name, "strategy": strategy, "closed_trades": 0,
            "verdict": "Inga avslutade affärer än.",
        }

    gross_moves = [p["gross_pct"] for p in pairs]
    net_pnls = [p["net_pnl"] for p in pairs]
    holds = [p["hold_minutes"] for p in pairs]

    gross_sum = sum(gross_moves)
    net_sum = sum(net_pnls)

    gross_wins = len([g for g in gross_moves if g > 0])
    net_wins = len([n for n in net_pnls if n > 0])

    flips = len([h for h in holds if h < FLIP_MINUTES])

    reasons = {}
    for p in pairs:
        key = p["reason"].split(":")[0].strip() or "okänd"
        reasons[key] = reasons.get(key, 0) + 1

    avg_gross = gross_sum / len(pairs)
    avg_net_pct = sum(p["net_pct"] for p in pairs) / len(pairs)
    friction_per_trade = avg_gross - avg_net_pct

    return {
        "bot": bot_name,
        "strategy": strategy,
        "closed_trades": len(pairs),
        # --- Kärnan i analysen ---
        "avg_gross_move_pct": round(avg_gross, 4),
        "avg_net_pct": round(avg_net_pct, 4),
        "friction_per_trade_pct": round(friction_per_trade, 4),
        "total_gross_pct": round(gross_sum, 2),
        "total_net_pnl": round(net_sum, 2),
        "total_fees_paid": round(total_fees, 2),
        # --- Träffsäkerhet före och efter kostnader ---
        "win_rate_gross_pct": round(gross_wins / len(pairs) * 100, 1),
        "win_rate_net_pct": round(net_wins / len(pairs) * 100, 1),
        # --- Handelstakt ---
        "avg_hold_minutes": round(statistics.mean(holds), 1),
        "median_hold_minutes": round(statistics.median(holds), 1),
        "flips_under_15min": flips,
        "flip_rate_pct": round(flips / len(pairs) * 100, 1),
        "exit_reasons": reasons,
        "verdict": _verdict(avg_gross, avg_net_pct, len(pairs), flips / len(pairs)),
    }


def _verdict(avg_gross: float, avg_net: float, trades: int, flip_rate: float) -> str:
    if trades < 20:
        return f"För få affärer ({trades}) för slutsats."

    if avg_gross > 0 and avg_net < 0:
        return (
            f"HAR KANT MEN ÄTS UPP: priset rör sig i rätt riktning "
            f"({avg_gross:+.3f}% per affär) men efter kostnader blir det "
            f"{avg_net:+.3f}%. Problemet är handelsfrekvensen, inte strategin. "
            f"Färre och längre affärer skulle kunna rädda den."
        )

    if avg_gross <= 0 and avg_net < 0:
        return (
            f"INGEN KANT: priset rör sig fel håll redan före kostnader "
            f"({avg_gross:+.3f}% per affär). Att sänka avgifterna hjälper inte — "
            f"strategin gissar fel."
        )

    if avg_net > 0:
        note = ""
        if flip_rate > 0.5:
            note = f" Men {flip_rate*100:.0f}% av affärerna är under 15 min — bräckligt."
        return f"LÖNSAM efter kostnader ({avg_net:+.3f}% per affär).{note}"

    return "Otydligt utfall."


def analyze_all() -> dict:
    """Analyserar alla bottar och sammanfattar mönstret."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, name, strategy FROM bots ORDER BY id")
        bots = cur.fetchall()

    results = [analyze_bot(b[0], b[1], b[2]) for b in bots]
    active = [r for r in results if r["closed_trades"] >= 20]

    # Sortera på nettoresultat
    active.sort(key=lambda r: r["total_net_pnl"], reverse=True)

    edge_but_costly = [r for r in active
                       if r["avg_gross_move_pct"] > 0 and r["avg_net_pct"] < 0]
    no_edge = [r for r in active if r["avg_gross_move_pct"] <= 0]
    profitable = [r for r in active if r["avg_net_pct"] > 0]

    total_trades = sum(r["closed_trades"] for r in active)
    total_fees = sum(r["total_fees_paid"] for r in active)
    avg_friction = (sum(r["friction_per_trade_pct"] for r in active) / len(active)
                    if active else 0)
    avg_hold = (sum(r["avg_hold_minutes"] for r in active) / len(active)
                if active else 0)

    return {
        "bots": results,
        "summary": {
            "bots_analyzed": len(active),
            "total_closed_trades": total_trades,
            "total_fees_paid": round(total_fees, 2),
            "avg_friction_per_trade_pct": round(avg_friction, 4),
            "avg_hold_minutes": round(avg_hold, 1),
            "profitable_after_costs": [r["bot"] for r in profitable],
            "edge_but_eaten_by_costs": [r["bot"] for r in edge_but_costly],
            "no_edge_at_all": [r["bot"] for r in no_edge],
            "diagnosis": _overall_diagnosis(
                active, edge_but_costly, no_edge, profitable, avg_hold, total_fees
            ),
        },
    }


def _overall_diagnosis(active, edge_but_costly, no_edge, profitable,
                       avg_hold, total_fees) -> str:
    if not active:
        return "För få avslutade affärer för att analysera."

    lines = []

    if len(edge_but_costly) > len(no_edge):
        lines.append(
            f"HUVUDPROBLEM: FRIKTION. {len(edge_but_costly)} av {len(active)} bottar "
            "rör sig i rätt riktning men förlorar efter avgifter och slippage. "
            "Det betyder att strategierna inte är felaktiga — de handlar för ofta."
        )
    elif len(no_edge) > len(edge_but_costly):
        lines.append(
            f"HUVUDPROBLEM: INGEN KANT. {len(no_edge)} av {len(active)} bottar gissar "
            "fel riktning redan före kostnader. Lägre avgifter räddar inte detta."
        )
    else:
        lines.append("Blandad bild — både friktion och riktningsfel förekommer.")

    if avg_hold < 30:
        lines.append(
            f"Genomsnittlig innehavstid är {avg_hold:.0f} minuter. Strategier som "
            "EMA-crossover och Bollinger är byggda för timmar och dagar. Att "
            "utvärdera dem varje minut ger whipsaw: signalen pendlar kring "
            "tröskeln och triggar köp och sälj om vartannat."
        )

    lines.append(f"Totalt betalt i avgifter: {total_fees:.2f} USDT.")

    if profitable:
        lines.append(f"Lönsamma efter kostnader: {', '.join(r['bot'] for r in profitable)}.")
    else:
        lines.append("Ingen bot är lönsam efter kostnader än.")

    return " ".join(lines)
