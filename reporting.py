"""
Kapitalkurvor och daglig rapport.

TVÅ SAKER SOM FIXAS HÄR:

1. RIKTIG DRAWDOWN
   Tidigare räknades drawdown bara på stängda affärer. Det missar det
   som faktiskt gör ont: en position som ligger 20% back men inte är
   såld syntes inte alls i statistiken. Nu sparas hela portföljvärdet
   regelbundet, så kurvan visar sanningen.

2. DAGLIG SAMMANFATTNING
   En rapport på morgonen istället för att du ska behöva öppna appen
   och tolka siffror. Den innehåller medvetet också det som gick dåligt —
   en rapport som bara visar vinster är marknadsföring, inte information.
"""
import logging
from datetime import datetime, timezone, timedelta

from db import get_cursor, get_ohlcv

logger = logging.getLogger("reporting")


def snapshot_all_bots():
    """Sparar nuvarande totalvärde för varje bot."""
    import bots

    for bot in bots.list_bots():
        try:
            positions = bots._get_positions(bot["id"])
            open_value = 0.0
            for p in positions:
                latest = get_ohlcv(p["symbol"], "1m", limit=1)
                price = float(latest[-1]["close"]) if latest else float(p["entry_price"])
                open_value += price * float(p["amount"])

            total = float(bot["quote_balance"]) + open_value

            with get_cursor() as cur:
                cur.execute(
                    "INSERT INTO bot_equity (bot_id, total_value, quote_balance, "
                    "positions_value) VALUES (%s, %s, %s, %s)",
                    (bot["id"], total, float(bot["quote_balance"]), open_value),
                )
        except Exception as e:
            logger.error("Equity-snapshot misslyckades för bot %s: %s", bot["name"], e)


def get_equity_curve(bot_id: int, hours: int = 168) -> list[dict]:
    """Kapitalkurva för en bot. Standard: senaste veckan."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT ts, total_value, quote_balance, positions_value FROM bot_equity "
            "WHERE bot_id = %s AND ts >= %s ORDER BY ts",
            (bot_id, cutoff),
        )
        cols = [c.name for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def true_drawdown(bot_id: int) -> dict:
    """
    Drawdown räknad på faktiskt portföljvärde, inklusive orealiserade
    förluster. Detta är den siffra som betyder något.
    """
    curve = get_equity_curve(bot_id, hours=8760)
    if len(curve) < 2:
        return {"available": False, "reason": "för få ögonblicksbilder än"}

    values = [float(c["total_value"]) for c in curve]
    peak = values[0]
    max_dd = 0.0
    peak_at = curve[0]["ts"]
    trough_at = curve[0]["ts"]
    current_peak_at = curve[0]["ts"]

    for i, v in enumerate(values):
        if v > peak:
            peak = v
            current_peak_at = curve[i]["ts"]
        dd = (peak - v) / peak * 100 if peak else 0
        if dd > max_dd:
            max_dd = dd
            peak_at = current_peak_at
            trough_at = curve[i]["ts"]

    current_dd = (peak - values[-1]) / peak * 100 if peak else 0

    return {
        "available": True,
        "max_drawdown_pct": round(max_dd, 2),
        "current_drawdown_pct": round(current_dd, 2),
        "peak_value": round(peak, 2),
        "current_value": round(values[-1], 2),
        "peak_at": peak_at,
        "trough_at": trough_at,
        "snapshots": len(curve),
    }


def build_daily_report() -> str:
    """Textrapport över senaste dygnet. Skickas till Telegram."""
    import bots
    import risk_manager
    import regime as regime_mod
    from config import settings

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    lines = []

    lines.append("📊 DYGNSRAPPORT")
    lines.append(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    lines.append("")

    # --- Marknadsregim ---
    try:
        reg = regime_mod.regime_for_symbols(settings.symbols)
        if reg.get("overall"):
            o = reg["overall"]
            lines.append(f"Marknad: {o['dominant_regime']} ({o['agreement']})")
            lines.append(f"Efficiency ratio: {o['avg_efficiency_ratio']}")
            lines.append("")
    except Exception as e:
        logger.debug("Regim i rapport misslyckades: %s", e)

    # --- Bottarnas dygnsresultat ---
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT b.name,
                   COUNT(*) FILTER (WHERE t.side = 'sell') AS closed,
                   COALESCE(SUM(t.realized_pnl), 0) AS pnl
            FROM bots b
            LEFT JOIN bot_trades t ON t.bot_id = b.id AND t.ts >= %s
            GROUP BY b.name
            ORDER BY pnl DESC
            """,
            (since,),
        )
        rows = cur.fetchall()

    active = [r for r in rows if r[1] > 0]

    if not active:
        lines.append("Inga affärer stängdes det senaste dygnet.")
        lines.append("Det är normalt — flera strategier handlar sällan.")
    else:
        lines.append(f"Affärer senaste dygnet: {sum(r[1] for r in active)}")
        lines.append("")
        for name, closed, pnl in active[:5]:
            emoji = "🟢" if float(pnl) > 0 else "🔴"
            lines.append(f"{emoji} {name}: {float(pnl):+.2f} USDT ({closed} affärer)")

        # Visa också det som gick sämst — inte bara vinnarna
        worst = active[-1]
        if worst not in active[:5] and float(worst[2]) < 0:
            lines.append("")
            lines.append(f"🔴 Sämst: {worst[0]} {float(worst[2]):+.2f} USDT")

    lines.append("")

    # --- Topplistan totalt ---
    try:
        lb = bots.leaderboard()
        summary = lb["summary"]
        lines.append(f"Totalt: {summary['total_closed_trades']} affärer "
                     f"på {summary['days_running']} dagar")
        baseline = summary.get("baseline_buy_and_hold_pct")
        if baseline is not None:
            lines.append(f"Buy & hold: {baseline:+.2f}%")
            beating = summary.get("beating_baseline", [])
            lines.append(f"Slår referensen: {len(beating)} bottar")
    except Exception as e:
        logger.debug("Topplista i rapport misslyckades: %s", e)

    # --- Riskläge ---
    try:
        state = risk_manager.get_risk_state()
        if state["kill_switch_active"]:
            lines.append("")
            lines.append(f"🛑 KILL SWITCH AKTIV: {state['kill_switch_reason']}")
    except Exception:
        pass

    return "\n".join(lines)


_last_report_date = None


def maybe_send_daily_report():
    """
    Körs regelbundet men skickar bara en gång per dygn, vid inställd timme.
    """
    global _last_report_date
    from config import settings
    from notifier import send_notification

    now = datetime.now(timezone.utc)
    if now.hour != settings.daily_report_hour_utc:
        return
    if _last_report_date == now.date():
        return

    try:
        report = build_daily_report()
        send_notification(report)
        _last_report_date = now.date()
        logger.info("Dygnsrapport skickad")
    except Exception as e:
        logger.error("Kunde inte skicka dygnsrapport: %s", e)
