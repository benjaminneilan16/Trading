
"""
Arbitrage-observatör med fullständig kostnadsmodell.

DEN HÄR MODULEN HANDLAR INTE. Den observerar och loggar, så att du efter
en mätperiod kan svara på frågan: uppstår det överhuvudtaget spreadar som
överlever kostnaderna, hur stora är de, och hur länge finns de kvar?

DEN VIKTIGASTE DESIGNPRINCIPEN, som du formulerade själv:

    Beräkna inte "spread = 2%, vinst = 4 USD".
    Simulera hela transaktionen och se vad som faktiskt blir kvar.

Skillnaden är enorm. En nominell spread på 2% kan vara en exekverbar
spread på -26% när gas, avgifter, slippage och pooldjup räknats in.

TVÅ HELT OLIKA PROBLEM, som du delade upp dem:

  A. TRANSFER-ARBITRAGE
     Köp på DEX -> flytta -> sälj på KuCoin.
     Betalar gas OCH uttagsavgift, och bär tidsrisken under hela
     överföringen. Spreaden måste överleva 10-30 minuter.

  B. INVENTORY-ARBITRAGE
     Kapital ligger på båda sidor. Köp på DEX och sälj på KuCoin
     samtidigt. Ingen tidsrisk i själva arbitrageögonblicket.

Vi mäter BÅDA, eftersom skillnaden mellan dem är hela poängen.

TRE SAKER SOM MODELLEN GÖR SYNLIGA — och som är lätta att missa:

1. Inventariet ÄR en position. För att sälja PEPE på KuCoin måste du redan
   äga PEPE där. Faller PEPE 30% förlorar du på lagret oavsett hur många
   lyckade arbitrage du gjort. Inventory-arbitrage är inte marknadsneutralt.

2. Rebalanseringen tar tillbaka kostnaden bakvägen. Du slipper gas och
   uttagsavgift i arbitrageögonblicket, men lagret driver — USDT hopar sig
   på ena sidan, tokens på andra. Förr eller senare måste du överföra.
   Kostnaden försvann inte, den fördelades ut över affärerna.

3. Gas per swap avgör vilken kedja som är möjlig. På Ethereum kostar en
   swap 5-30 USD; på Solana ungefär 0,01 USD. Med ett kapital på tiotusentals
   kronor är Solana i praktiken den enda kedjan där matematiken går ihop.
"""
import logging
from datetime import datetime, timezone, timedelta

from db import get_cursor

logger = logging.getLogger("arbitrage")

# --- Gaskostnad per swap, i USD -------------------------------------------
# Grova uppskattningar. De varierar med nätverksbelastning, och på Ethereum
# kan de vara flera gånger högre under hög aktivitet.
#
# Att de skiljer sig med en faktor 1000 mellan kedjorna är inte en detalj —
# det avgör vilken kedja som är användbar alls vid ditt kapital.
GAS_COST_USD = {
    "ethereum": 12.0,
    "solana": 0.02,
    "bsc": 0.25,
    "base": 0.05,
    "arbitrum": 0.15,
    "polygon": 0.02,
    "optimism": 0.10,
    "avalanche": 0.15,
}
DEFAULT_GAS_USD = 5.0

# DEX-swapavgift. De flesta AMM:er tar 0,25-0,30%.
DEX_SWAP_FEE_PCT = 0.30

# KuCoins handelsavgift (taker)
KUCOIN_FEE_PCT = 0.10

# Under detta är kandidaten inte värd att logga som intressant
MIN_INTERESTING_NET_PCT = 0.0


def ensure_tables():
    with get_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS arbitrage_observations (
                id                    BIGSERIAL PRIMARY KEY,
                ts                    TIMESTAMPTZ NOT NULL DEFAULT now(),
                symbol                TEXT        NOT NULL,
                chain                 TEXT,
                trade_size_usd        NUMERIC     NOT NULL,
                dex_price             NUMERIC,
                kucoin_price          NUMERIC,
                nominal_spread_pct    NUMERIC,
                direction             TEXT,
                dex_liquidity_usd     NUMERIC,
                dex_slippage_pct      NUMERIC,
                kucoin_spread_pct     NUMERIC,
                gas_cost_usd          NUMERIC,
                withdrawal_fee_usd    NUMERIC,
                total_cost_pct        NUMERIC,
                inventory_net_pct     NUMERIC,
                transfer_net_pct      NUMERIC,
                inventory_profitable  BOOLEAN,
                transfer_profitable   BOOLEAN,
                transferable          BOOLEAN,
                notes                 TEXT
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_arb_obs_ts ON arbitrage_observations (ts DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_arb_obs_symbol ON arbitrage_observations (symbol, ts DESC)"
        )


def estimate_dex_slippage(trade_size_usd: float, liquidity_usd: float) -> float:
    """
    Uppskattat prisgenomslag i en AMM-pool.

    För en konstant-produkt-pool (x*y=k) gäller ungefär att genomslaget är
    handelsstorleken delad med reserven på den sida du handlar mot. Med
    total likviditet L är reserven ungefär L/2, och genomsnittligt erhållet
    pris ligger halvvägs — vilket ger slippage ≈ size / liquidity.

    Det är en förenkling, men den fångar det viktigaste: slippage växer
    linjärt med storleken och är omvänt proportionell mot pooldjupet. En
    affär på 200 USD i en pool på 20 000 USD kostar cirka 1%.
    """
    if not liquidity_usd or liquidity_usd <= 0:
        return 100.0  # okänd likviditet behandlas som oanvändbar
    return min(trade_size_usd / liquidity_usd * 100, 100.0)


def _kucoin_spread_pct(symbol: str) -> float | None:
    """Faktisk spread från senaste orderbokssnapshot."""
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT spread_pct FROM orderbook_snapshots WHERE symbol = %s "
            "ORDER BY ts DESC LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _kucoin_price(symbol: str) -> float | None:
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT close FROM ohlcv WHERE symbol = %s AND timeframe = '1m' "
            "ORDER BY ts DESC LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
    return float(row[0]) if row else None


def _withdrawal_fee_usd(symbol: str, price_usd: float) -> tuple[float | None, bool]:
    """
    Uttagsavgift omräknad till USD, plus om överföring är möjlig alls.
    KuCoin anger avgiften i tokenens egen enhet.
    """
    import token_identity
    t = token_identity.check_transferability(symbol)
    if not t["transferable"]:
        return None, False

    fees = [n["withdrawal_min_fee"] for n in t["usable_chains"]
            if n["withdrawal_min_fee"] is not None]
    if not fees or not price_usd:
        return None, True
    return float(min(fees)) * price_usd, True


def observe(symbol: str, trade_size_usd: float = 200.0) -> dict | None:
    """
    Jämför DEX-pris mot KuCoin-pris och räknar ut vad som faktiskt blir
    kvar efter hela kedjan av kostnader. Loggar men handlar inte.
    """
    ensure_tables()

    # --- Hämta priser ---
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT price_usd, liquidity_usd, chain_id FROM token_features "
            "WHERE symbol = %s AND ts >= now() - INTERVAL '30 minutes' "
            "ORDER BY ts DESC LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()

    if not row or not row[0]:
        return None

    dex_price = float(row[0])
    liquidity = float(row[1]) if row[1] else 0.0
    chain = (row[2] or "").lower()

    kucoin_price = _kucoin_price(symbol)
    if not kucoin_price:
        return None

    # --- Nominell spread ---
    nominal_pct = (kucoin_price - dex_price) / dex_price * 100
    direction = "buy_dex_sell_kucoin" if nominal_pct > 0 else "buy_kucoin_sell_dex"

    # --- Kostnader ---
    gas = GAS_COST_USD.get(chain, DEFAULT_GAS_USD)
    gas_pct = gas / trade_size_usd * 100

    dex_slip = estimate_dex_slippage(trade_size_usd, liquidity)
    kucoin_spread = _kucoin_spread_pct(symbol) or 0.1
    # Du korsar halva spreaden när du tar marknadspris
    kucoin_slip = kucoin_spread / 2

    wd_fee_usd, transferable = _withdrawal_fee_usd(symbol, kucoin_price)
    wd_pct = (wd_fee_usd / trade_size_usd * 100) if wd_fee_usd else None

    # --- Inventory-arbitrage: ingen överföring i själva ögonblicket ---
    inventory_cost_pct = (
        gas_pct            # swap på DEX kostar gas oavsett
        + DEX_SWAP_FEE_PCT
        + KUCOIN_FEE_PCT
        + dex_slip
        + kucoin_slip
    )
    inventory_net = abs(nominal_pct) - inventory_cost_pct

    # --- Transfer-arbitrage: gas + uttagsavgift + tidsrisk ---
    transfer_cost_pct = inventory_cost_pct + (wd_pct if wd_pct is not None else 999)
    transfer_net = abs(nominal_pct) - transfer_cost_pct

    notes = []
    if not transferable:
        notes.append("Ingen kedja med både insättning och uttag påslaget")
    if liquidity and liquidity < trade_size_usd * 20:
        notes.append(f"Tunn pool: {liquidity:,.0f} USD för en affär på {trade_size_usd:,.0f}")
    if chain == "ethereum":
        notes.append(f"Ethereum-gas ({gas} USD) motsvarar {gas_pct:.2f}% av affären")

    result = {
        "symbol": symbol,
        "chain": chain,
        "trade_size_usd": trade_size_usd,
        "dex_price": dex_price,
        "kucoin_price": kucoin_price,
        "nominal_spread_pct": round(nominal_pct, 4),
        "direction": direction,
        "dex_liquidity_usd": liquidity,
        "dex_slippage_pct": round(dex_slip, 4),
        "kucoin_spread_pct": round(kucoin_spread, 4),
        "gas_cost_usd": gas,
        "withdrawal_fee_usd": round(wd_fee_usd, 4) if wd_fee_usd else None,
        "total_cost_pct": round(inventory_cost_pct, 4),
        "inventory_net_pct": round(inventory_net, 4),
        "transfer_net_pct": round(transfer_net, 4) if wd_pct is not None else None,
        "inventory_profitable": inventory_net > MIN_INTERESTING_NET_PCT,
        "transfer_profitable": (transfer_net > MIN_INTERESTING_NET_PCT
                                if wd_pct is not None else False),
        "transferable": transferable,
        "notes": " | ".join(notes) if notes else None,
    }

    _store(result)
    return result


def _store(r: dict):
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO arbitrage_observations
                (symbol, chain, trade_size_usd, dex_price, kucoin_price,
                 nominal_spread_pct, direction, dex_liquidity_usd, dex_slippage_pct,
                 kucoin_spread_pct, gas_cost_usd, withdrawal_fee_usd, total_cost_pct,
                 inventory_net_pct, transfer_net_pct, inventory_profitable,
                 transfer_profitable, transferable, notes)
            VALUES (%(symbol)s,%(chain)s,%(trade_size_usd)s,%(dex_price)s,
                    %(kucoin_price)s,%(nominal_spread_pct)s,%(direction)s,
                    %(dex_liquidity_usd)s,%(dex_slippage_pct)s,%(kucoin_spread_pct)s,
                    %(gas_cost_usd)s,%(withdrawal_fee_usd)s,%(total_cost_pct)s,
                    %(inventory_net_pct)s,%(transfer_net_pct)s,
                    %(inventory_profitable)s,%(transfer_profitable)s,
                    %(transferable)s,%(notes)s)
            """,
            r,
        )


def observe_all(symbols: list[str], trade_size_usd: float = 200.0) -> dict:
    """Kör observationen för alla symboler som har både DEX- och KuCoin-data."""
    results = []
    for sym in symbols:
        try:
            r = observe(sym, trade_size_usd)
            if r:
                results.append(r)
        except Exception as e:
            logger.error("Arbitrage-observation misslyckades för %s: %s", sym, e)

    profitable = [r for r in results if r["inventory_profitable"]]
    if profitable:
        logger.info("Lönsamma inventory-kandidater: %s",
                    ", ".join(f"{r['symbol']} {r['inventory_net_pct']:+.2f}%"
                              for r in profitable))

    return {
        "observed": len(results),
        "inventory_profitable": len(profitable),
        "transfer_profitable": len([r for r in results if r["transfer_profitable"]]),
    }


# ---------------------------------------------------------------------------
# Sammanfattning — svaret på om detta är värt att bygga vidare på
# ---------------------------------------------------------------------------

def summary(hours: int = 168) -> dict:
    """
    Efter en mätperiod: uppstår det spreadar som överlever kostnaderna?

    Det här är hela poängen med observatören. Är svaret "två gånger på
    åtta dagar, 0,3%, borta inom en minut" har du sparat veckor av
    byggarbete. Är svaret "flera gånger om dagen på Solana, 1-2%" —
    då vet du att det är värt att bygga exekveringen, och exakt hur den
    ska dimensioneras.
    """
    ensure_tables()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE inventory_profitable), "
            "COUNT(*) FILTER (WHERE transfer_profitable) "
            "FROM arbitrage_observations WHERE ts >= %s",
            (cutoff,),
        )
        total, inv_ok, trans_ok = cur.fetchone()

        cur.execute(
            "SELECT AVG(ABS(nominal_spread_pct)), AVG(total_cost_pct), "
            "MAX(inventory_net_pct) FROM arbitrage_observations WHERE ts >= %s",
            (cutoff,),
        )
        avg_spread, avg_cost, best_net = cur.fetchone()

        cur.execute(
            """
            SELECT symbol, chain, COUNT(*) AS obs,
                   COUNT(*) FILTER (WHERE inventory_profitable) AS profitable,
                   AVG(inventory_net_pct) AS avg_net,
                   MAX(inventory_net_pct) AS best_net,
                   AVG(dex_liquidity_usd) AS avg_liq
            FROM arbitrage_observations
            WHERE ts >= %s
            GROUP BY symbol, chain
            ORDER BY profitable DESC, avg_net DESC
            LIMIT 20
            """,
            (cutoff,),
        )
        cols = [c.name for c in cur.description]
        per_symbol = [dict(zip(cols, r)) for r in cur.fetchall()]

        # Fördelning per kedja — visar om gaskostnaden avgör allt
        cur.execute(
            """
            SELECT chain, COUNT(*) AS obs,
                   COUNT(*) FILTER (WHERE inventory_profitable) AS profitable,
                   AVG(gas_cost_usd) AS avg_gas,
                   AVG(total_cost_pct) AS avg_cost_pct
            FROM arbitrage_observations WHERE ts >= %s
            GROUP BY chain ORDER BY obs DESC
            """,
            (cutoff,),
        )
        cols = [c.name for c in cur.description]
        per_chain = [dict(zip(cols, r)) for r in cur.fetchall()]

    return {
        "period_hours": hours,
        "observations": total,
        "inventory_profitable": inv_ok,
        "transfer_profitable": trans_ok,
        "hit_rate_pct": round(inv_ok / total * 100, 2) if total else 0,
        "avg_nominal_spread_pct": round(float(avg_spread), 3) if avg_spread else None,
        "avg_total_cost_pct": round(float(avg_cost), 3) if avg_cost else None,
        "best_net_pct": round(float(best_net), 3) if best_net else None,
        "per_symbol": per_symbol,
        "per_chain": per_chain,
        "verdict": _verdict(total, inv_ok, trans_ok, avg_spread, avg_cost, hours),
    }


def _verdict(total, inv_ok, trans_ok, avg_spread, avg_cost, hours) -> str:
    if not total:
        return ("Inga observationer än. Kräver att både on-chain features och "
                "KuCoin-data finns för samma symbol.")

    days = hours / 24
    parts = []

    if avg_spread and avg_cost:
        parts.append(
            f"Genomsnittlig nominell spread {float(avg_spread):.3f}%, "
            f"genomsnittlig kostnad {float(avg_cost):.3f}%."
        )
        if float(avg_cost) > float(avg_spread):
            parts.append(
                "Kostnaden överstiger spreaden i genomsnitt — de flesta "
                "observerade prisskillnader är alltså inte exekverbara."
            )

    if inv_ok == 0:
        parts.append(
            f"INGEN lönsam inventory-kandidat på {days:.1f} dagar och {total} "
            "observationer. Det är det förväntade utfallet, och det tydligaste "
            "svaret du kan få: prisskillnaderna finns just för att de är dyrare "
            "att fånga än de är värda."
        )
    else:
        rate = inv_ok / total * 100
        parts.append(
            f"{inv_ok} av {total} observationer ({rate:.1f}%) hade positiv "
            f"inventory-marginal över {days:.1f} dagar."
        )
        if rate < 1:
            parts.append("Under 1% — troligen brus eller tillfälliga datafel snarare än en kant.")
        else:
            parts.append(
                "Värt att titta närmare på. Kolla per_chain: är träffarna "
                "koncentrerade till lågkostnadskedjor är det ett riktigt mönster. "
                "Kom också ihåg att inventariet är en riktig position — du är "
                "lång i varje token du håller lager i."
            )

    if trans_ok == 0 and total:
        parts.append("Noll lönsamma transfer-kandidater: uttagsavgifterna ensamma äter marginalen.")

    return " ".join(parts)
