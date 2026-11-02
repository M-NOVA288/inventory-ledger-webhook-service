"""Demo data: three sites, four products, starting balances and thresholds."""

from __future__ import annotations

from .db import Database

SITES = [
    ("HOU1", "Houston - Bayport DC"),
    ("DAL1", "Dallas - Commerce St Warehouse"),
    ("LRD1", "Laredo - Border Cross-Dock"),
]

PRODUCTS = [
    ("PC-10432", "Polyethylene Resin (bag)", "bag"),
    ("PC-20981", "Antioxidant Additive (drum)", "drum"),
    ("PC-30112", "Stretch Film Roll", "roll"),
    ("RG-4410", "Epoxy Hardener (drum)", "drum"),
]

# (site, sku, on_hand, reorder_point)
STOCK = [
    ("HOU1", "PC-10432", 480, 150), ("HOU1", "PC-20981", 60, 25), ("HOU1", "PC-30112", 900, 300),
    ("DAL1", "PC-10432", 210, 100), ("DAL1", "PC-30112", 120, 100), ("DAL1", "RG-4410", 30, 10),
    ("LRD1", "PC-20981", 40, 20), ("LRD1", "RG-4410", 18, 12),
]


def seed(db: Database) -> None:
    for code, name in SITES:
        db.execute("INSERT INTO sites (site_code, name) VALUES (?, ?) ON CONFLICT DO NOTHING", (code, name))
    for sku, desc, unit in PRODUCTS:
        db.execute(
            "INSERT INTO products (sku, description, unit) VALUES (?, ?, ?) ON CONFLICT DO NOTHING",
            (sku, desc, unit),
        )
    for site, sku, on_hand, reorder in STOCK:
        db.execute(
            "INSERT INTO stock_levels (site_code, sku, on_hand, reorder_point) VALUES (?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            (site, sku, on_hand, reorder),
        )
    db.commit()
