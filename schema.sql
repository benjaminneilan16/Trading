-- Schema för Market Data Engine (Fas 1)
-- Kör med: psql -U postgres -d crypto_trading -f schema.sql

CREATE TABLE IF NOT EXISTS ohlcv (
    id          BIGSERIAL PRIMARY KEY,
    exchange    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    timeframe   TEXT        NOT NULL,       -- t.ex. '1m', '5m', '1h'
    ts          TIMESTAMPTZ NOT NULL,       -- candle open time
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
    bids        JSONB       NOT NULL,       -- [[price, amount], ...] topp N nivåer
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
    side        TEXT        NOT NULL,       -- 'buy' / 'sell'
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
    id              BIGSERIAL PRIMARY KEY,
    exchange        TEXT        NOT NULL,
    symbol          TEXT        NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    open_interest   NUMERIC     NOT NULL,
    open_interest_usd NUMERIC,
    UNIQUE (exchange, symbol, ts)
);

-- Liquidations läggs till i ett senare steg (kräver websocket-stream,
-- se README för hur du bygger vidare på detta).
CREATE TABLE IF NOT EXISTS liquidations (
    id          BIGSERIAL PRIMARY KEY,
    exchange    TEXT        NOT NULL,
    symbol      TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    side        TEXT        NOT NULL,
    price       NUMERIC     NOT NULL,
    amount      NUMERIC     NOT NULL
);

-- ---------------------------------------------------------------------
-- Paper trading (Fas 2 simulerad + Fas 9) — låtsaspengar, riktiga priser
-- ---------------------------------------------------------------------

-- En enda rad per "konto". quote_balance = låtsas-USDT du har kvar att handla för.
CREATE TABLE IF NOT EXISTS paper_wallet (
    id              INT PRIMARY KEY DEFAULT 1,
    quote_currency  TEXT    NOT NULL DEFAULT 'USDT',
    quote_balance   NUMERIC NOT NULL,
    starting_balance NUMERIC NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (id = 1)  -- garanterar bara en rad, det finns bara ett konto
);

-- Öppna positioner, en rad per symbol du äger just nu i papperskontot
CREATE TABLE IF NOT EXISTS paper_positions (
    symbol          TEXT PRIMARY KEY,
    amount          NUMERIC NOT NULL,       -- hur mycket av basvalutan (t.ex. BTC) du äger
    avg_entry_price NUMERIC NOT NULL,       -- snittpris du köpte in dig på
    opened_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Momentum-strategin behöver spåra högsta pris sedan entry (för trailing stop)
-- och vilken strategi som öppnade positionen.
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS peak_price NUMERIC;
ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS strategy TEXT DEFAULT 'technical';

-- Träffar från momentum-scannern, så du kan se vad den hittade även när
-- den valde att inte gå in.
CREATE TABLE IF NOT EXISTS scanner_hits (
    id                  BIGSERIAL PRIMARY KEY,
    symbol              TEXT        NOT NULL,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    score               NUMERIC     NOT NULL,
    volume_ratio        NUMERIC,
    price_change_15m_pct NUMERIC,
    quote_volume_24h    NUMERIC,
    spread_pct          NUMERIC,
    accelerating        BOOLEAN,
    entered             BOOLEAN     NOT NULL DEFAULT FALSE,
    reason              TEXT
);
CREATE INDEX IF NOT EXISTS idx_scanner_hits_ts ON scanner_hits (ts DESC);

-- Logg över varje simulerad affär, för att se hur strategin presterat över tid
CREATE TABLE IF NOT EXISTS paper_trades (
    id              BIGSERIAL PRIMARY KEY,
    symbol          TEXT        NOT NULL,
    side            TEXT        NOT NULL,    -- 'buy' / 'sell'
    price           NUMERIC     NOT NULL,
    amount          NUMERIC     NOT NULL,    -- mängd i basvaluta
    quote_amount    NUMERIC     NOT NULL,    -- mängd i USDT (price * amount)
    realized_pnl    NUMERIC,                 -- endast ifyllt vid 'sell'
    reason          TEXT,                    -- t.ex. "EMA-crossover + RSI 28 + MACD bullish"
    ts              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_ts ON paper_trades (ts DESC);

-- Logg över VARJE beslut strategin tar (även "avvakta"), så du kan se
-- resonemanget bakom — bra för att lita på/felsöka den automatiska logiken.
CREATE TABLE IF NOT EXISTS strategy_signals (
    id          BIGSERIAL PRIMARY KEY,
    symbol      TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    decision    TEXT        NOT NULL,   -- 'buy' / 'sell' / 'hold'
    score       NUMERIC     NOT NULL,   -- sammanvägd signalstyrka
    ema_fast    NUMERIC,
    ema_slow    NUMERIC,
    rsi         NUMERIC,
    macd        NUMERIC,
    macd_signal NUMERIC,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS idx_strategy_signals_symbol_ts ON strategy_signals (symbol, ts DESC);
