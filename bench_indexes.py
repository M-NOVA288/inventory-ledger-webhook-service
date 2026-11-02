"""Measure what the composite ledger index buys on a realistic filter query.

Builds the same ledger twice (with and without indexes), loads N movement rows
spread across sites, SKUs and dates, then times a multi-variable filter:
one site + one SKU + a date range, newest first.

    python bench_indexes.py --rows 300000
"""

from __future__ import annotations

import argparse
import random
import statistics
import time
from datetime import datetime, timedelta

from inventory_service.db import Database

SITES = [f"S{n:02d}" for n in range(1, 21)]
SKUS = [f"PC-{n:05d}" for n in range(1, 501)]
TYPES = ["receipt", "shipment", "adjustment"]

QUERY = (
    "SELECT id, quantity_delta, balance_after, created_at FROM ledger "
    "WHERE site_code = ? AND sku = ? AND created_at BETWEEN ? AND ? "
    "ORDER BY created_at DESC LIMIT 100"
)


def build(rows: int, with_indexes: bool, seed: int = 42) -> Database:
    db = Database("sqlite:///:memory:")
    db.init_schema(with_indexes=with_indexes)
    rng = random.Random(seed)
    start = datetime(2025, 1, 1)
    batch = []
    for _ in range(rows):
        ts = start + timedelta(minutes=rng.randrange(0, 60 * 24 * 365))
        batch.append((rng.choice(SITES), rng.choice(SKUS), rng.choice(TYPES),
                      rng.randint(-50, 50), rng.randint(0, 1000), ts.isoformat(sep=" ")))
    db.conn.executemany(
        "INSERT INTO ledger (site_code, sku, movement_type, quantity_delta, balance_after, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        batch,
    )
    db.commit()
    return db


def time_queries(db: Database, runs: int, seed: int = 7) -> float:
    rng = random.Random(seed)
    timings = []
    for _ in range(runs):
        params = (rng.choice(SITES), rng.choice(SKUS), "2025-03-01", "2025-09-30")
        t0 = time.perf_counter()
        db.query(QUERY, params)
        timings.append((time.perf_counter() - t0) * 1000)
    return statistics.median(timings)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=300_000)
    parser.add_argument("--runs", type=int, default=200)
    args = parser.parse_args()

    results = {}
    for label, indexed in (("no index", False), ("composite index", True)):
        db = build(args.rows, indexed)
        results[label] = time_queries(db, args.runs)
        plan = db.query("EXPLAIN QUERY PLAN " + QUERY, ("S01", "PC-00001", "2025-03-01", "2025-09-30"))
        print(f"{label:>16}: median {results[label]:.3f} ms   plan: {plan[0]['detail']}")
        db.close()

    speedup = results["no index"] / results["composite index"]
    print(f"\n{args.rows:,} ledger rows, {args.runs} queries each: {speedup:.0f}x faster with the index")


if __name__ == "__main__":
    main()
