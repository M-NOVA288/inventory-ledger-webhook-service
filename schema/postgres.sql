-- PostgreSQL schema for the inventory ledger service.
-- Mirrors the SQLite schema in inventory_service/db.py.

CREATE TABLE IF NOT EXISTS sites (
    site_code   TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    sku         TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT 'ea'
);

CREATE TABLE IF NOT EXISTS stock_levels (
    site_code     TEXT NOT NULL REFERENCES sites(site_code),
    sku           TEXT NOT NULL REFERENCES products(sku),
    on_hand       INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    reorder_point INTEGER NOT NULL DEFAULT 0 CHECK (reorder_point >= 0),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (site_code, sku)
);

CREATE TABLE IF NOT EXISTS ledger (
    id             BIGSERIAL PRIMARY KEY,
    site_code      TEXT NOT NULL,
    sku            TEXT NOT NULL,
    movement_type  TEXT NOT NULL CHECK (movement_type IN ('receipt','shipment','adjustment')),
    quantity_delta INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    reference      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id            BIGSERIAL PRIMARY KEY,
    site_code     TEXT NOT NULL,
    sku           TEXT NOT NULL,
    on_hand       INTEGER NOT NULL,
    reorder_point INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ledger_site_sku_time ON ledger (site_code, sku, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_type_time     ON ledger (movement_type, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_status        ON alerts (status);
-- Partial index: the "what's below threshold right now" dashboard query.
CREATE INDEX IF NOT EXISTS idx_stock_below_threshold
    ON stock_levels (site_code) WHERE on_hand <= reorder_point;
