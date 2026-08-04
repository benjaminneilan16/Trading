-- ===================================================================
-- KOMPLETT DATABASSCHEMA — Market Data Engine
--
-- Kör detta EN gång i Railway -> Postgres -> Console.
-- Innehåller allt: marknadsdata, paper trading, bot-arena, risk,
-- nya listningar. Säkert att köra flera gånger (allt är IF NOT EXISTS).
--
-- Ersätter alla tidigare migration_*.sql-filer.
-- ===================================================================


-- -------------------------------------------------------------------
-- MARKNADSDATA (Fas 1)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ohlcv (
    id          BIGSERIAL PRIMARY KEY,
    exchange    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    timeframe   TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    open        NUMERIC     NOT NULL,
    high        NUMERIC     NOT NULL,
    low         NUMERIC     NOT NULL,
    close       NUMERIC     NOT NULL,
    volume      NUMERIC     NOT NULL,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (exchange, symbol, timeframe, ts)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_ts ON ohlcv (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    exchange    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    bids        JSONB       NOT NULL,
    asks        JSONB       NOT NULL,
    best_bid    NUMERIC,
    best_ask    NUMERIC,
    spread_pct  NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_orderbook_symbol_ts ON orderbook_snapshots (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS trades (
    id          BIGSERIAL PRIMARY KEY,
    exchange    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    trade_id    TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    side        TEXT        NOT NULL,
    price       NUMERIC     NOT NULL,
    amount      NUMERIC     NOT NULL,
    UNIQUE (exchange, symbol, trade_id)
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS funding_rates (
    id            BIGSERIAL PRIMARY KEY,
    exchange      TEXT        NOT NULL,
    symbol        TEXT        NOT NULL,
    ts            TIMESTAMPTZ NOT NULL,
    funding_rate  NUMERIC     NOT NULL,
    next_funding  TIMESTAMPTZ,
    UNIQUE (exchange, symbol, ts)
);

CREATE TABLE IF NOT EXISTS open_interest (
    id                BIGSERIAL PRIMARY KEY,
    exchange          TEXT        NOT NULL,
    symbol            TEXT        NOT NULL,
    ts                TIMESTAMPTZ NOT NULL,
    open_interest     NUMERIC     NOT NULL,
    open_interest_usd NUMERIC,
    UNIQUE (exchange, symbol, ts)
);

CREATE TABLE IF NOT EXISTS liquidations (
    id          BIGSERIAL PRIMARY KEY,
    exchange    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    side        TEXT        NOT NULL,
    price       NUMERIC     NOT NULL,
    amount      NUMERIC     NOT NULL
);


-- -------------------------------------------------------------------
-- PAPER TRADING (momentum-scannern)
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS paper_wallet (
    id               INT PRIMARY KEY DEFAULT 1,
    quote_currency   TEXT    NOT NULL DEFAULT 'USDT',
    quote_balance    NUMERIC NOT NULL,
    starting_balance NUMERIC NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS paper_positions (
    symbol          TEXT PRIMARY KEY,
    amount          NUMERIC NOT NULL,
    avg_entry_price NUMERIC NOT NULL,
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    peak_price      NUMERIC,
    strategy        TEXT DEFAULT 'technical',
    stop_loss_pct   NUMERIC,
    partial_taken   BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id           BIGSERIAL PRIMARY KEY,
    symbol       TEXT        NOT NULL,
    side         TEXT        NOT NULL,
    price        NUMERIC     NOT NULL,
    amount       NUMERIC     NOT NULL,
    quote_amount NUMERIC     NOT NULL,
    realized_pnl NUMERIC,
    reason       TEXT,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_ts ON paper_trades (ts DESC);

CREATE TABLE IF NOT EXISTS strategy_signals (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision    TEXT        NOT NULL,
    score       NUMERIC     NOT NULL,
    ema_fast    NUMERIC,
    ema_slow    NUMERIC,
    rsi         NUMERIC,
    macd        NUMERIC,
    macd_signal NUMERIC,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_strategy_signals_symbol_ts ON strategy_signals (symbol, ts DESC);

CREATE TABLE IF NOT EXISTS scanner_hits (
    id                   BIGSERIAL PRIMARY KEY,
    symbol               TEXT        NOT NULL,
    ts                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    score                NUMERIC     NOT NULL,
    volume_ratio         NUMERIC,
    price_change_15m_pct NUMERIC,
    quote_volume_24h     NUMERIC,
    spread_pct           NUMERIC,
    accelerating         BOOLEAN,
    entered              BOOLEAN     NOT NULL DEFAULT FALSE,
    reason               TEXT
);
CREATE INDEX IF NOT EXISTS idx_scanner_hits_ts ON scanner_hits (ts DESC);


-- -------------------------------------------------------------------
-- BOT-ARENAN
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bots (
    id                SERIAL PRIMARY KEY,
    name              TEXT        NOT NULL UNIQUE,
    strategy          TEXT        NOT NULL,
    params            JSONB       NOT NULL DEFAULT '{}',
    enabled           BOOLEAN     NOT NULL DEFAULT TRUE,
    quote_balance     NUMERIC     NOT NULL,
    starting_balance  NUMERIC     NOT NULL,
    position_size_pct NUMERIC     NOT NULL DEFAULT 0.20,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_positions (
    id          BIGSERIAL PRIMARY KEY,
    bot_id      INT         NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    symbol      TEXT        NOT NULL,
    amount      NUMERIC     NOT NULL,
    entry_price NUMERIC     NOT NULL,
    peak_price  NUMERIC,
    opened_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (bot_id, symbol)
);

CREATE TABLE IF NOT EXISTS bot_trades (
    id           BIGSERIAL PRIMARY KEY,
    bot_id       INT         NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    symbol       TEXT        NOT NULL,
    side         TEXT        NOT NULL,
    price        NUMERIC     NOT NULL,
    amount       NUMERIC     NOT NULL,
    quote_amount NUMERIC     NOT NULL,
    realized_pnl NUMERIC,
    fees_paid    NUMERIC     NOT NULL DEFAULT 0,
    reason       TEXT,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_trades_bot_ts ON bot_trades (bot_id, ts DESC);

CREATE TABLE IF NOT EXISTS bot_equity (
    id              BIGSERIAL PRIMARY KEY,
    bot_id          INT         NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_value     NUMERIC     NOT NULL,
    quote_balance   NUMERIC     NOT NULL,
    positions_value NUMERIC     NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bot_equity_bot_ts ON bot_equity (bot_id, ts);


-- -------------------------------------------------------------------
-- RISK MANAGER
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS risk_state (
    id                 INT PRIMARY KEY DEFAULT 1,
    kill_switch_active BOOLEAN     NOT NULL DEFAULT FALSE,
    kill_switch_reason TEXT,
    kill_switch_at     TIMESTAMPTZ,
    CHECK (id = 1)
);

INSERT INTO risk_state (id, kill_switch_active)
VALUES (1, FALSE)
ON CONFLICT (id) DO NOTHING;


-- -------------------------------------------------------------------
-- NYA LISTNINGAR
-- -------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS token_registry (
    symbol           TEXT PRIMARY KEY,
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    age_days         INT,
    age_checked_at   TIMESTAMPTZ,
    quote_volume_24h NUMERIC,
    is_new_listing   BOOLEAN NOT NULL DEFAULT FALSE
);


-- -------------------------------------------------------------------
-- KOLUMNER SOM LAGTS TILL I EFTERHAND
-- (ofarliga att köra även på en färsk databas)
-- -------------------------------------------------------------------

ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS peak_price NUMERIC;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS strategy TEXT DEFAULT 'technical';
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS stop_loss_pct NUMERIC;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS partial_taken BOOLEAN DEFAULT FALSE;
