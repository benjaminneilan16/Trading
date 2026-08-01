"""
Momentum-strategi med snabba exits.

Skiljer sig från strategy.py (den lugna EMA/RSI/MACD-strategin) på ett
avgörande sätt: här är EXIT-reglerna viktigare än entry-reglerna.

Att fånga en token som börjar röra sig är bara halva jobbet. Det som
avgör om strategin tjänar eller förlorar pengar är hur snabbt du kommer
ut när det vänder — dessa rörelser dör ofta lika snabbt som de startar.

Fyra exit-regler, den som slår först vinner:
  1. TAKE PROFIT     — nådde målet, ta hem vinsten
  2. STOP LOSS       — det gick fel, ut direkt
  3. TRAILING STOP   — låser in vinst om priset backar från toppen
  4. TIME EXIT       — rörelsen dog ut, positionen är "död pengar"
"""
import logging

logger = logging.getLogger("momentum")

# --- Entry -----------------------------------------------------------------
MIN_ENTRY_SCORE = 2.0        # scanner-score som krävs för att gå in

# --- Exit ------------------------------------------------------------------
TAKE_PROFIT_PCT = 4.0        # ta hem vinst vid +4%
STOP_LOSS_PCT = -2.0         # ut vid -2% (mindre än vinstmålet = positiv risk/reward)
TRAILING_STOP_PCT = 2.0      # om priset backat 2% från högsta noterade -> ut
MAX_HOLD_MINUTES = 45        # dör rörelsen ut, ut ändå

# Hur ofta exit-reglerna kollas (sekunder) — detta måste vara TÄTT,
# annars hinner rörelsen vända innan vi reagerar.
EXIT_CHECK_INTERVAL = 30


def should_enter(candidate: dict, hype: dict | None = None) -> tuple[bool, str]:
    """
    Avgör om en scanner-kandidat är värd att gå in i.
    `hype` är valfri — om social.py får en riktig datakälla senare kan
    den förstärka eller dämpa beslutet utan att resten behöver ändras.
    """
    score = candidate["score"]
    reason = candidate["reason"]

    if hype and hype.get("available"):
        score += hype["score"]  # -1.0 .. +1.0
        reason += f", hype {hype['score']:+.2f}"

    if score < MIN_ENTRY_SCORE:
        return False, f"score {score:.1f} under tröskeln"

    return True, f"score {score:.1f}: {reason}"


def check_exit(position: dict, current_price: float, held_minutes: float) -> tuple[bool, str]:
    """
    Kollar alla exit-regler för en öppen position.

    position behöver: avg_entry_price, peak_price
    Returnerar (ska_sälja, anledning)
    """
    entry = float(position["avg_entry_price"])
    peak = float(position.get("peak_price") or entry)

    pnl_pct = (current_price - entry) / entry * 100

    # 1. Take profit
    if pnl_pct >= TAKE_PROFIT_PCT:
        return True, f"TAKE PROFIT: +{pnl_pct:.2f}%"

    # 2. Stop loss
    if pnl_pct <= STOP_LOSS_PCT:
        return True, f"STOP LOSS: {pnl_pct:.2f}%"

    # 3. Trailing stop — bara aktiv om vi varit i vinst
    if peak > entry:
        drop_from_peak_pct = (peak - current_price) / peak * 100
        if drop_from_peak_pct >= TRAILING_STOP_PCT:
            return True, (
                f"TRAILING STOP: -{drop_from_peak_pct:.2f}% från topp "
                f"(totalt {pnl_pct:+.2f}%)"
            )

    # 4. Time exit
    if held_minutes >= MAX_HOLD_MINUTES:
        return True, f"TIME EXIT: {held_minutes:.0f} min utan utfall ({pnl_pct:+.2f}%)"

    return False, f"håller kvar ({pnl_pct:+.2f}%)"
