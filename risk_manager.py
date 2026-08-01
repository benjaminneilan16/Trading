"""
Risk Manager (Fas 3) — skyddsnätet.

Grundprincipen: strategierna bestämmer VAD som ska köpas. Risk Manager
bestämmer OM det får köpas, och hur mycket. Ingen strategi får gå förbi
den här filen.

Varför det är byggt som en grindvakt och inte som regler inbakade i varje
strategi: när du senare lägger till fler strategier vill du inte behöva
komma ihåg att kopiera in riskreglerna i var och en. Här finns de på ETT
ställe, och kan inte glömmas bort.

Fem lager av skydd:
  1. Positionsstorlek   — hur mycket får satsas per affär
  2. Exponeringstak     — hur mycket av kontot får vara i marknaden samtidigt
  3. Daglig förlustgräns— stänger av handeln om dagen går för dåligt
  4. Kill switch        — manuell eller automatisk total nödstopp
  5. Rug-pull-detektor  — nödutgång vid tecken på kollaps
"""
import logging
from datetime import datetime, timezone, timedelta

from db import get_cursor
from config import settings

logger = logging.getLogger("risk")


# ---------------------------------------------------------------------------
# Kill switch — persistent i databasen så den överlever omstarter
# ---------------------------------------------------------------------------

def ensure_risk_state():
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk_state (id, kill_switch_active)
            VALUES (1, FALSE)
            ON CONFLICT (id) DO NOTHING
            """
        )


def get_risk_state() -> dict:
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT kill_switch_active, kill_switch_reason, kill_switch_at "
            "FROM risk_state WHERE id = 1"
        )
        row = cur.fetchone()
    if row is None:
        return {"kill_switch_active": False, "kill_switch_reason": None, "kill_switch_at": None}
    return {
        "kill_switch_active": row[0],
        "kill_switch_reason": row[1],
        "kill_switch_at": row[2],
    }


def activate_kill_switch(reason: str):
    """Stoppar ALL ny handel. Öppna positioner stängs separat av caller."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO risk_state (id, kill_switch_active, kill_switch_reason, kill_switch_at)
            VALUES (1, TRUE, %s, now())
            ON CONFLICT (id) DO UPDATE SET
                kill_switch_active = TRUE,
                kill_switch_reason = EXCLUDED.kill_switch_reason,
                kill_switch_at = now()
            """,
            (reason,),
        )
    logger.warning("KILL SWITCH AKTIVERAD: %s", reason)


def deactivate_kill_switch():
    with get_cursor() as cur:
        cur.execute(
            "UPDATE risk_state SET kill_switch_active = FALSE, "
            "kill_switch_reason = NULL, kill_switch_at = NULL WHERE id = 1"
        )
    logger.info("Kill switch avaktiverad")


# ---------------------------------------------------------------------------
# Daglig statistik
# ---------------------------------------------------------------------------

def get_daily_stats() -> dict:
    """
    Dagens resultat, räknat från midnatt UTC.
    Bara STÄNGDA affärer räknas (realized_pnl), eftersom orealiserad
    vinst kan försvinna innan den blir verklig.
    """
    start_of_day = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT
                COALESCE(SUM(realized_pnl), 0),
                COUNT(*) FILTER (WHERE side = 'sell'),
                COUNT(*) FILTER (WHERE side = 'sell' AND realized_pnl > 0),
                COUNT(*) FILTER (WHERE side = 'sell' AND realized_pnl <= 0)
            FROM paper_trades
            WHERE ts >= %s AND side = 'sell'
            """,
            (start_of_day,),
        )
        pnl, closed, wins, losses = cur.fetchone()

        cur.execute("SELECT starting_balance FROM paper_wallet WHERE id = 1")
        row = cur.fetchone()
        starting_balance = float(row[0]) if row else settings.paper_starting_balance

    pnl = float(pnl)
    return {
        "date": start_of_day.date().isoformat(),
        "realized_pnl": pnl,
        "realized_pnl_pct": pnl / starting_balance * 100 if starting_balance else 0.0,
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": (wins / closed * 100) if closed else None,
    }


def daily_loss_limit_hit() -> tuple[bool, str]:
    stats = get_daily_stats()
    limit = -abs(settings.max_daily_loss_pct)
    if stats["realized_pnl_pct"] <= limit:
        return True, (
            f"Daglig förlustgräns nådd: {stats['realized_pnl_pct']:.2f}% "
            f"(gräns {limit:.2f}%), {stats['losses']} förluster idag"
        )
    return False, ""


# ---------------------------------------------------------------------------
# Grindvakten — varje köp måste passera här
# ---------------------------------------------------------------------------

def check_entry(symbol: str, portfolio: dict, intended_size_pct: float) -> dict:
    """
    Avgör om ett köp får genomföras, och med vilken storlek.

    Returnerar:
        {"allowed": bool, "size_pct": float, "reason": str}

    size_pct kan vara MINDRE än intended_size_pct om exponeringstaket
    tvingar ner den — hellre en mindre position än ingen alls.
    """
    # 1. Kill switch
    state = get_risk_state()
    if state["kill_switch_active"]:
        return {"allowed": False, "size_pct": 0, "reason": f"Kill switch aktiv: {state['kill_switch_reason']}"}

    # 2. Daglig förlustgräns
    hit, reason = daily_loss_limit_hit()
    if hit:
        # Aktivera kill switch automatiskt — annars fortsätter den försöka
        # varje scan-cykel resten av dagen.
        activate_kill_switch(reason)
        return {"allowed": False, "size_pct": 0, "reason": reason}

    total_value = portfolio["total_value"]
    quote_balance = portfolio["quote_balance"]
    positions = portfolio["positions"]

    if total_value <= 0:
        return {"allowed": False, "size_pct": 0, "reason": "Kontot är tomt"}

    # 3. Max antal samtidiga positioner
    if len(positions) >= settings.max_open_positions:
        return {
            "allowed": False, "size_pct": 0,
            "reason": f"Max antal positioner nått ({settings.max_open_positions})",
        }

    # 4. Äger vi redan denna token?
    if any(p["symbol"] == symbol for p in positions):
        return {"allowed": False, "size_pct": 0, "reason": f"Har redan position i {symbol}"}

    # 5. Exponeringstak — hur mycket av kontot är redan i marknaden?
    in_market = sum(p["value_usdt"] for p in positions)
    exposure_pct = in_market / total_value * 100
    max_exposure = settings.max_total_exposure_pct

    if exposure_pct >= max_exposure:
        return {
            "allowed": False, "size_pct": 0,
            "reason": f"Exponering {exposure_pct:.1f}% >= tak {max_exposure:.1f}%",
        }

    # Hur mycket utrymme finns kvar innan taket?
    room_pct = max_exposure - exposure_pct
    intended_pct_of_total = intended_size_pct * 100

    size_pct = intended_size_pct
    note = ""
    if intended_pct_of_total > room_pct:
        size_pct = room_pct / 100
        note = f" (nedskalad från {intended_pct_of_total:.1f}% pga exponeringstak)"

    # 6. Minsta vettiga affärsstorlek — annars äter avgifterna upp allt
    spend = quote_balance * size_pct
    if spend < settings.min_trade_size_usdt:
        return {
            "allowed": False, "size_pct": 0,
            "reason": f"Affären skulle bli {spend:.2f} USDT, under minimum "
                      f"{settings.min_trade_size_usdt} USDT",
        }

    # 7. Cooldown — förlorade vi nyss på just denna token?
    if _in_cooldown(symbol):
        return {
            "allowed": False, "size_pct": 0,
            "reason": f"{symbol} i cooldown efter nylig förlust",
        }

    return {
        "allowed": True,
        "size_pct": size_pct,
        "reason": f"Godkänd: exponering {exposure_pct:.1f}% -> "
                  f"{exposure_pct + size_pct*100:.1f}%{note}",
    }


def _in_cooldown(symbol: str) -> bool:
    """
    Hindrar boten från att köpa tillbaka in i samma token direkt efter en
    förlust. Utan detta kan den fastna i en loop: köper, stoppas ut,
    ser samma signal igen, köper igen — och blöder ihjäl på avgifter.
    """
    if settings.loss_cooldown_minutes <= 0:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.loss_cooldown_minutes)
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT 1 FROM paper_trades
            WHERE symbol = %s AND side = 'sell' AND realized_pnl < 0 AND ts >= %s
            LIMIT 1
            """,
            (symbol, cutoff),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Dynamisk stop loss
# ---------------------------------------------------------------------------

def dynamic_stop_loss_pct(atr_pct: float | None, base_stop_pct: float) -> float:
    """
    Anpassar stop loss efter tokenens volatilitet.

    En token som normalt svänger 6% i timmen kommer trigga en 2%-stop av
    rent brus, långt innan något faktiskt gått fel. Här sätts stoppen
    istället till ~1,5x tokenens normala rörelse, inom rimliga gränser.
    """
    if atr_pct is None:
        return base_stop_pct

    suggested = -(atr_pct * 1.5)
    # Aldrig snävare än basvärdet, aldrig bredare än taket
    return max(min(suggested, base_stop_pct), -abs(settings.max_stop_loss_pct))


# ---------------------------------------------------------------------------
# Rug-pull / kollapsdetektor — nödutgång
# ---------------------------------------------------------------------------

def check_rug_pull(exchange, symbol: str, entry_price: float) -> tuple[bool, str]:
    """
    Letar efter tecken på att en token kollapsar just nu, snarare än att
    bara röra sig neråt. Vid träff ska positionen ut OMEDELBART, utan att
    vänta på vanliga exit-regler.

    Tre tecken:
      1. Prisras — kraftig nedgång på mycket kort tid
      2. Spread-explosion — likviditeten försvinner (svårt att komma ut)
      3. Volymkollaps efter spik — köparna är borta
    """
    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe="1m", limit=15)
        ticker = exchange.fetch_ticker(symbol)
    except Exception as e:
        logger.debug("Rug-pull-check misslyckades för %s: %s", symbol, e)
        return False, ""

    if len(candles) < 10:
        return False, ""

    closes = [c[4] for c in candles]
    volumes = [c[5] for c in candles]

    # 1. Prisras senaste 3 minuterna
    drop_3m = (closes[-1] - closes[-4]) / closes[-4] * 100
    if drop_3m <= -settings.rug_pull_drop_pct:
        return True, f"RUG-PULL: prisras {drop_3m:.1f}% på 3 min"

    # 2. Spread-explosion
    bid, ask = ticker.get("bid"), ticker.get("ask")
    if bid and ask and bid > 0:
        spread_pct = (ask - bid) / bid * 100
        if spread_pct >= settings.rug_pull_spread_pct:
            return True, f"RUG-PULL: spread {spread_pct:.2f}% (likviditeten försvinner)"

    # 3. Volymkollaps — hade spik, nu dött, och vi ligger back
    peak_volume = max(volumes[:-3]) if len(volumes) > 3 else 0
    recent_volume = sum(volumes[-3:]) / 3
    pnl_pct = (closes[-1] - entry_price) / entry_price * 100

    if peak_volume > 0 and recent_volume < peak_volume * 0.15 and pnl_pct < -1:
        return True, (
            f"RUG-PULL: volymen dog ({recent_volume/peak_volume*100:.0f}% av toppen), "
            f"position {pnl_pct:+.1f}%"
        )

    return False, ""


# ---------------------------------------------------------------------------
# Sammanfattning för dashboarden
# ---------------------------------------------------------------------------

def risk_summary(portfolio: dict) -> dict:
    state = get_risk_state()
    daily = get_daily_stats()

    total_value = portfolio.get("total_value", 0)
    positions = portfolio.get("positions", [])
    in_market = sum(p["value_usdt"] for p in positions)
    exposure_pct = (in_market / total_value * 100) if total_value else 0

    return {
        "kill_switch_active": state["kill_switch_active"],
        "kill_switch_reason": state["kill_switch_reason"],
        "kill_switch_at": state["kill_switch_at"],
        "daily": daily,
        "exposure_pct": exposure_pct,
        "open_positions": len(positions),
        "limits": {
            "max_daily_loss_pct": settings.max_daily_loss_pct,
            "max_open_positions": settings.max_open_positions,
            "max_total_exposure_pct": settings.max_total_exposure_pct,
            "max_position_size_pct": settings.max_position_size_pct * 100,
            "min_trade_size_usdt": settings.min_trade_size_usdt,
            "loss_cooldown_minutes": settings.loss_cooldown_minutes,
            "max_stop_loss_pct": settings.max_stop_loss_pct,
        },
        "headroom": {
            "daily_loss_remaining_pct": settings.max_daily_loss_pct + daily["realized_pnl_pct"],
            "exposure_remaining_pct": settings.max_total_exposure_pct - exposure_pct,
            "position_slots_free": max(0, settings.max_open_positions - len(positions)),
        },
    }
