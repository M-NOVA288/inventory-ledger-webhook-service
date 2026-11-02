"""Database connection and schema.

Runs on SQLite out of the box (zero setup). Set DATABASE_URL to a postgres://
URL and install psycopg to run against PostgreSQL instead; the SQL is written
to work on both, and schema/postgres.sql holds the Postgres DDL.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS sites (
    site_code   TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    sku         TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    unit        TEXT NOT NULL DEFAULT 'ea'
);

-- Current on-hand quantity per site/product, plus the safety threshold.
CREATE TABLE IF NOT EXISTS stock_levels (
    site_code     TEXT NOT NULL REFERENCES sites(site_code),
    sku           TEXT NOT NULL REFERENCES products(sku),
    on_hand       INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    reorder_point INTEGER NOT NULL DEFAULT 0 CHECK (reorder_point >= 0),
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (site_code, sku)
);

-- Append-only movement history. Never updated or deleted.
CREATE TABLE IF NOT EXISTS ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    site_code      TEXT NOT NULL,
    sku            TEXT NOT NULL,
    movement_type  TEXT NOT NULL CHECK (movement_type IN ('receipt','shipment','adjustment')),
    quantity_delta INTEGER NOT NULL,
    balance_after  INTEGER NOT NULL,
    reference      TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    site_code    TEXT NOT NULL,
    sku          TEXT NOT NULL,
    on_hand      INTEGER NOT NULL,
    reorder_point INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Composite index matching the most common filter: one site + one SKU over time.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_ledger_site_sku_time ON ledger (site_code, sku, created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_type_time     ON ledger (movement_type, created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_status        ON alerts (status);
"""


class Database:
    """Thin wrapper so the rest of the code doesn't care which engine is underneath."""

    def __init__(self, url: str | None = None):
        url = url or os.environ.get("DATABASE_URL", "sqlite:///inventory.db")
        self.is_postgres = url.startswith(("postgres://", "postgresql://"))
        if self.is_postgres:
            import psycopg  # optional dependency, only needed for Postgres

            self.conn = psycopg.connect(url, autocommit=False)
        else:
            path = url.removeprefix("sqlite:///") or ":memory:"
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")

    def init_schema(self, with_indexes: bool = True) -> None:
        if self.is_postgres:
            ddl = (Path(__file__).resolve().parent.parent / "schema" / "postgres.sql").read_text()
            with self.conn.cursor() as cur:
                cur.execute(ddl)
        else:
            self.conn.executescript(SCHEMA_SQLITE + (INDEXES if with_indexes else ""))
        self.conn.commit()

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    def execute(self, sql: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()
