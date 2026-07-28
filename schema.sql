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
