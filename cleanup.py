"""
Datastädning — hindrar databasen från att fyllas.

PROBLEMET SOM UPPSTOD: `orderbook_snapshots` sparar 20 prisnivåer per
sida som JSONB, var 10-30:e sekund, per symbol. Med tio symboler blir det
tiotusentals rader per dygn, var och en flera kilobyte. Databasen nådde
95% på några dagar.

INSIKTEN: nästan ingen av den datan används. Order flow-analysen tittar
på senaste orderbokssnapshotten och 15 minuter bakåt i trades. Allt äldre
än så ligger bara och tar plats.

VAD SOM SPARAS OCH VARFÖR:

  ohlcv              — SPARAS FÖR ALLTID. Liten (en rad per minut och
                       symbol) och nödvändig för strategier och backtest.
  bot_trades         — SPARAS. Detta ÄR din mätdata.
  bot_equity         — SPARAS. Kapitalkurvorna.
  paper_trades       — SPARAS.
  token_registry     — SPARAS. Litet.

  orderbook_snapshots — 6 timmar. Bara senaste används.
  trades              — 3 dagar. Order flow tittar 15 minuter bakåt.
  scanner_hits        — 14 dagar.
  strategy_signals    — 14 dagar.

VIKTIGT OM DISKUTRYMME: DELETE frigör inte utrymme till operativsystemet,
det markerar bara rader som döda så att nya rader kan återanvända platsen.
Det räcker för att hålla storleken STABIL, vilket är vad vi vill. Att
faktiskt krympa filen kräver VACUUM FULL, som behöver ledigt utrymme
motsvarande tabellens storlek — det gör man en gång vid nödläge, inte
löpande.
"""
import logging
from datetime import datetime, timezone

from db import get_cursor

logger = logging.getLogger("cleanup")

import os

RETENTION = {
    "orderbook_snapshots": int(os.getenv("RETAIN_ORDERBOOK_HOURS", "6")),
    "trades": int(os.getenv("RETAIN_TRADES_HOURS", "72")),
    "scanner_hits": int(os.getenv("RETAIN_SCANNER_HOURS", "336")),      # 14 dagar
    "strategy_signals": int(os.getenv("RETAIN_SIGNALS_HOURS", "336")),
}


def run_cleanup() -> dict:
    """Raderar data äldre än respektive gräns. Körs regelbundet av motorn."""
    results = {}

    for table, hours in RETENTION.items():
        try:
            with get_cursor() as cur:
                cur.execute(
                    f"DELETE FROM {table} WHERE ts < now() - INTERVAL '%s hours'" % hours
                )
                deleted = cur.rowcount
            results[table] = deleted
            if deleted:
                logger.info("Städade %s: %d rader borttagna (äldre än %dh)",
                            table, deleted, hours)
        except Exception as e:
            logger.error("Städning misslyckades för %s: %s", table, e)
            results[table] = f"fel: {e}"

    return results


def database_size() -> dict:
    """Storlek per tabell — för att se vad som växer."""
    with get_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT relname,
                   pg_total_relation_size(relid) AS bytes,
                   pg_size_pretty(pg_total_relation_size(relid)) AS pretty,
                   n_live_tup
            FROM pg_stat_user_tables
            ORDER BY pg_total_relation_size(relid) DESC
            """
        )
        tables = [
            {"table": r[0], "bytes": r[1], "size": r[2], "rows": r[3]}
            for r in cur.fetchall()
        ]

        cur.execute("SELECT pg_database_size(current_database())")
        total_bytes = cur.fetchone()[0]

    return {
        "total_bytes": total_bytes,
        "total_pretty": f"{total_bytes / 1024 / 1024:.1f} MB",
        "tables": tables,
        "retention_hours": RETENTION,
        "largest": tables[0]["table"] if tables else None,
    }


def emergency_truncate_orderbook() -> dict:
    """
    Tömmer orderbook_snapshots helt och frigör utrymmet direkt.

    Används när databasen är nästan full. TRUNCATE frigör plats
    omedelbart, till skillnad från DELETE som kräver en efterföljande
    VACUUM FULL — och en VACUUM FULL behöver ledigt utrymme motsvarande
    tabellens storlek, vilket man per definition inte har i det läget.

    Datan är säker att kasta: order flow använder bara senaste snapshotten.
    """
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT COUNT(*) FROM orderbook_snapshots")
        before = cur.fetchone()[0]
        cur.execute("SELECT pg_total_relation_size('orderbook_snapshots')")
        size_before = cur.fetchone()[0]

    with get_cursor() as cur:
        cur.execute("TRUNCATE TABLE orderbook_snapshots")

    logger.warning("NÖDTÖMNING: orderbook_snapshots tömd (%d rader, %.1f MB frigjort)",
                   before, size_before / 1024 / 1024)

    return {
        "rows_deleted": before,
        "freed_mb": round(size_before / 1024 / 1024, 1),
        "note": "Order flow använder bara senaste snapshotten — inget mätvärde förlorat.",
    }
