"""
Order Flow — läser vad köpare och säljare FAKTISKT gör, just nu.

Skillnaden mot teknisk analys: EMA och RSI räknar på priset, som är
resultatet. Order flow tittar på orsaken — vem som köper, hur mycket, och
hur aggressivt. Det är så professionella traders faktiskt läser marknaden,
och det är den ärligaste versionen av "följa vad andra gör".

All data kommer från tabeller du redan samlar in: `trades` och
`orderbook_snapshots`. Ingen ny datakälla, inga API-nycklar.

SEX SIGNALER:

1. CVD (Cumulative Volume Delta)
   Aggressiva köp minus aggressiva säljningar. En "buy" i trades-tabellen
   betyder att någon tog priset från säljsidan — de ville in NU, till
   marknadspris. Det är otålighet, och otålighet är information.

2. Orderboksobalans
   Ligger det mer volym på köp- eller säljsidan? Tung köpsida betyder
   stöd under priset.

3. Valprintar (stora affärer)
   Enstaka affärer långt över den normala storleken. En affär på 50x
   medianen kommer inte från en privatperson.

4. Absorption
   Hög volym men priset rör sig knappt. Någon köper allt som säljs, utan
   att jaga priset uppåt. Detta är ofta den starkaste signalen som finns —
   det är hur stora aktörer bygger positioner utan att avslöja sig.

5. Affärsstorleksfördelning
   Många små affärer = privatpersoner. Få stora = institutioner.

6. Aggressionsratio
   Hur stor andel av volymen som var aggressiva köp.
"""
import logging
import statistics
from datetime import datetime, timezone, timedelta

from db import get_cursor

logger = logging.getLogger("orderflow")

# En affär räknas som "valprint" om den är minst så här många gånger
# större än medianaffären för samma symbol i samma fönster.
WHALE_MULTIPLE = 8.0

# Absorption: minst så här hög volym jämfört med normalt, men mindre
# prisrörelse än så här många procent.
ABSORPTION_VOLUME_MULT = 2.0
ABSORPTION_MAX_MOVE_PCT = 0.15


def get_flow_metrics(symbol: str, window_minutes: int = 15) -> dict:
    """
    Räknar ut alla order flow-signaler för en symbol.
    Returnerar ett dict med råvärden + en sammanvägd score (-1 till +1).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    prev_cutoff = cutoff - timedelta(minutes=window_minutes)

    with get_cursor(commit=False) as cur:
        # Affärer i det aktuella fönstret
        cur.execute(
            "SELECT side, price, amount, ts FROM trades "
            "WHERE symbol = %s AND ts >= %s ORDER BY ts",
            (symbol, cutoff),
        )
        rows = cur.fetchall()

        # Föregående fönster, för att kunna jämföra
        cur.execute(
            "SELECT COALESCE(SUM(price * amount), 0) FROM trades "
            "WHERE symbol = %s AND ts >= %s AND ts < %s",
            (symbol, prev_cutoff, cutoff),
        )
        prev_volume = float(cur.fetchone()[0] or 0)

        # Senaste orderbokssnapshot
        cur.execute(
            "SELECT bids, asks, best_bid, best_ask, ts FROM orderbook_snapshots "
            "WHERE symbol = %s ORDER BY ts DESC LIMIT 1",
            (symbol,),
        )
        book = cur.fetchone()

    if len(rows) < 10:
        return {
            "available": False,
            "reason": f"För få affärer ({len(rows)}) i fönstret — "
                      "vänta tills mer data samlats in",
            "score": 0.0,
        }

    # --- 1. CVD: aggressiva köp minus aggressiva säljningar ---
    buy_volume = sum(float(r[1]) * float(r[2]) for r in rows if r[0] == "buy")
    sell_volume = sum(float(r[1]) * float(r[2]) for r in rows if r[0] == "sell")
    total_volume = buy_volume + sell_volume
    cvd = buy_volume - sell_volume
    cvd_ratio = cvd / total_volume if total_volume else 0.0

    # --- 2. Orderboksobalans ---
    book_imbalance = None
    spread_pct = None
    if book:
        bids, asks = book[0], book[1]
        bid_depth = sum(float(b[1]) for b in bids) if bids else 0
        ask_depth = sum(float(a[1]) for a in asks) if asks else 0
        if bid_depth + ask_depth > 0:
            book_imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)
        if book[2] and book[3] and float(book[2]) > 0:
            spread_pct = (float(book[3]) - float(book[2])) / float(book[2]) * 100

    # --- 3. Valprintar ---
    sizes = [float(r[1]) * float(r[2]) for r in rows]
    median_size = statistics.median(sizes)
    whale_threshold = median_size * WHALE_MULTIPLE

    whale_buys = sum(
        1 for r in rows
        if r[0] == "buy" and float(r[1]) * float(r[2]) >= whale_threshold
    )
    whale_sells = sum(
        1 for r in rows
        if r[0] == "sell" and float(r[1]) * float(r[2]) >= whale_threshold
    )
    whale_buy_volume = sum(
        float(r[1]) * float(r[2]) for r in rows
        if r[0] == "buy" and float(r[1]) * float(r[2]) >= whale_threshold
    )
    whale_sell_volume = sum(
        float(r[1]) * float(r[2]) for r in rows
        if r[0] == "sell" and float(r[1]) * float(r[2]) >= whale_threshold
    )
    whale_net = whale_buy_volume - whale_sell_volume

    # --- 4. Absorption ---
    first_price = float(rows[0][1])
    last_price = float(rows[-1][1])
    price_move_pct = (last_price - first_price) / first_price * 100 if first_price else 0

    volume_vs_prev = total_volume / prev_volume if prev_volume > 0 else 1.0
    absorption = (
        volume_vs_prev >= ABSORPTION_VOLUME_MULT
        and abs(price_move_pct) <= ABSORPTION_MAX_MOVE_PCT
    )
    # Vem absorberar? Om köpvolymen dominerar under absorption bygger
    # någon en lång position.
    absorption_side = None
    if absorption:
        absorption_side = "buy" if cvd_ratio > 0 else "sell"

    # --- 5. Affärsstorleksfördelning ---
    avg_size = sum(sizes) / len(sizes)
    # Skevhet: hög kvot mellan snitt och median betyder att några få
    # stora affärer drar upp snittet — institutionell närvaro.
    size_skew = avg_size / median_size if median_size else 1.0

    # --- 6. Sammanvägd score ---
    score = 0.0
    reasons = []

    if abs(cvd_ratio) > 0.15:
        contribution = max(min(cvd_ratio * 2, 0.6), -0.6)
        score += contribution
        direction = "köp" if cvd_ratio > 0 else "sälj"
        reasons.append(f"CVD {cvd_ratio:+.0%} ({direction}stryck)")

    if book_imbalance is not None and abs(book_imbalance) > 0.2:
        contribution = max(min(book_imbalance * 0.5, 0.3), -0.3)
        score += contribution
        side = "köpsidan" if book_imbalance > 0 else "säljsidan"
        reasons.append(f"orderbok tung på {side} ({book_imbalance:+.0%})")

    if whale_net != 0 and total_volume > 0:
        whale_impact = whale_net / total_volume
        contribution = max(min(whale_impact * 1.5, 0.5), -0.5)
        score += contribution
        if whale_buys or whale_sells:
            reasons.append(
                f"{whale_buys} stora köp / {whale_sells} stora säljningar"
            )

    if absorption:
        # Absorption är den starkaste signalen — hög volym, priset står still
        contribution = 0.4 if absorption_side == "buy" else -0.4
        score += contribution
        reasons.append(
            f"ABSORPTION: {volume_vs_prev:.1f}x volym men priset rör sig "
            f"bara {price_move_pct:+.2f}% — någon köper allt"
            if absorption_side == "buy" else
            f"ABSORPTION: {volume_vs_prev:.1f}x volym, priset står still — "
            "någon säljer in i styrka"
        )

    score = max(min(score, 1.0), -1.0)

    return {
        "available": True,
        "symbol": symbol,
        "window_minutes": window_minutes,
        "score": round(score, 3),
        "reason": " | ".join(reasons) if reasons else "neutralt flöde",
        "trades_analyzed": len(rows),
        "cvd": round(cvd, 2),
        "cvd_ratio": round(cvd_ratio, 3),
        "buy_volume": round(buy_volume, 2),
        "sell_volume": round(sell_volume, 2),
        "total_volume": round(total_volume, 2),
        "volume_vs_previous": round(volume_vs_prev, 2),
        "book_imbalance": round(book_imbalance, 3) if book_imbalance is not None else None,
        "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
        "whale_buys": whale_buys,
        "whale_sells": whale_sells,
        "whale_net_volume": round(whale_net, 2),
        "whale_threshold_usdt": round(whale_threshold, 2),
        "median_trade_usdt": round(median_size, 2),
        "size_skew": round(size_skew, 2),
        "absorption": absorption,
        "absorption_side": absorption_side,
        "price_move_pct": round(price_move_pct, 3),
    }


def get_flow_for_symbols(symbols: list[str], window_minutes: int = 15) -> dict:
    """Order flow för flera symboler — används av bot-arenan."""
    return {s: get_flow_metrics(s, window_minutes) for s in symbols}
