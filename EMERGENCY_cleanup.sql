-- ===================================================================
-- AKUT: databasen är full. Kör detta i Railway -> Postgres -> Data.
-- ===================================================================

-- STEG 1: Se var utrymmet tar vägen. Kör denna FÖRST.
SELECT
    relname AS tabell,
    pg_size_pretty(pg_total_relation_size(relid)) AS storlek,
    n_live_tup AS rader
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC;


-- ===================================================================
-- STEG 2: Frigör utrymme. Kör raden nedan.
--
-- orderbook_snapshots är nästan säkert boven: 20 prisnivåer per sida,
-- sparade som JSONB, var 10-30:e sekund, per symbol.
--
-- Order flow-analysen använder bara SENASTE snapshotten, så historiken
-- fyller ingen funktion. Den är säker att kasta.
--
-- Varför TRUNCATE och inte DELETE: DELETE markerar bara rader som döda
-- och frigör INTE diskutrymme förrän en VACUUM FULL körs — och en
-- VACUUM FULL behöver ledigt utrymme motsvarande tabellens storlek,
-- vilket du inte har vid 95%. TRUNCATE frigör direkt.
-- ===================================================================

TRUNCATE TABLE orderbook_snapshots;


-- ===================================================================
-- STEG 3: Kör STEG 1 igen och kontrollera att utrymmet frigjorts.
--
-- Räcker det inte, kasta även gamla trades (order flow tittar bara
-- 15 minuter bakåt, så en vecka är gott om marginal):
-- ===================================================================

-- DELETE FROM trades WHERE ts < now() - INTERVAL '2 days';
-- VACUUM (ANALYZE) trades;

-- Och gamla scanner-träffar och signaler:
-- DELETE FROM scanner_hits WHERE ts < now() - INTERVAL '7 days';
-- DELETE FROM strategy_signals WHERE ts < now() - INTERVAL '7 days';
-- VACUUM (ANALYZE) scanner_hits;
-- VACUUM (ANALYZE) strategy_signals;

-- RÖR INTE dessa — de är din faktiska mätdata:
--   ohlcv, bots, bot_trades, bot_positions, bot_equity,
--   paper_trades, paper_wallet, token_registry
