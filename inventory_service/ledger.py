"""Inventory business rules: record movements, keep balances, detect breaches."""

from __future__ import annotations

from .db import Database

MOVEMENT_TYPES = {"receipt", "shipment", "adjustment"}


class InventoryError(ValueError):
    """A request that breaks a business rule (bad input, unknown SKU, overdraw)."""


def record_movement(
    db: Database,
    site_code: str,
    sku: str,
    movement_type: str,
    quantity: int,
    reference: str | None = None,
) -> dict:
    """Apply a stock movement atomically.

    receipts add stock, shipments remove it, adjustments take a signed quantity
    (e.g. -3 after a cycle count). Returns the new balance and, if this
    movement pushed stock to or below the reorder point, an alert id.
    """
    if movement_type not in MOVEMENT_TYPES:
        raise InventoryError(f"movement_type must be one of {sorted(MOVEMENT_TYPES)}")
    if not isinstance(quantity, int) or isinstance(quantity, bool):
        raise InventoryError("quantity must be an integer")
    if movement_type in {"receipt", "shipment"} and quantity <= 0:
        raise InventoryError(f"{movement_type} quantity must be positive")
    if movement_type == "adjustment" and quantity == 0:
        raise InventoryError("adjustment quantity cannot be zero")

    delta = -quantity if movement_type == "shipment" else quantity

    rows = db.query(
        "SELECT on_hand, reorder_point FROM stock_levels WHERE site_code = ? AND sku = ?",
        (site_code, sku),
    )
    if not rows:
        raise InventoryError(f"no stock record for site {site_code}, sku {sku}")
    before, reorder_point = rows[0]["on_hand"], rows[0]["reorder_point"]
    after = before + delta
    if after < 0:
        raise InventoryError(
            f"insufficient stock at {site_code} for {sku}: on hand {before}, requested {quantity}"
        )

    try:
        db.execute(
            "UPDATE stock_levels SET on_hand = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE site_code = ? AND sku = ?",
            (after, site_code, sku),
        )
        db.execute(
            "INSERT INTO ledger (site_code, sku, movement_type, quantity_delta, balance_after, reference) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (site_code, sku, movement_type, delta, after, reference),
        )
        alert_id = None
        # Alert only on the crossing, not on every movement while already low.
        if before > reorder_point >= after:
            cur = db.execute(
                "INSERT INTO alerts (site_code, sku, on_hand, reorder_point) VALUES (?, ?, ?, ?)"
                + (" RETURNING id" if db.is_postgres else ""),
                (site_code, sku, after, reorder_point),
            )
            alert_id = cur.fetchone()[0] if db.is_postgres else cur.lastrowid
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "site_code": site_code,
        "sku": sku,
        "on_hand": after,
        "reorder_point": reorder_point,
        "below_threshold": after <= reorder_point,
        "alert_id": alert_id,
    }


def set_reorder_point(db: Database, site_code: str, sku: str, reorder_point: int) -> dict:
    if not isinstance(reorder_point, int) or reorder_point < 0:
        raise InventoryError("reorder_point must be a non-negative integer")
    cur = db.execute(
        "UPDATE stock_levels SET reorder_point = ? WHERE site_code = ? AND sku = ?",
        (reorder_point, site_code, sku),
    )
    if cur.rowcount == 0:
        raise InventoryError(f"no stock record for site {site_code}, sku {sku}")
    db.commit()
    return get_stock(db, site_code, sku)


def get_stock(db: Database, site_code: str, sku: str) -> dict | None:
    rows = db.query(
        "SELECT s.site_code, s.sku, p.description, s.on_hand, s.reorder_point, s.updated_at "
        "FROM stock_levels s JOIN products p ON p.sku = s.sku "
        "WHERE s.site_code = ? AND s.sku = ?",
        (site_code, sku),
    )
    return rows[0] if rows else None


def list_stock(db: Database, site_code: str | None = None, below_threshold: bool = False) -> list[dict]:
    sql = (
        "SELECT s.site_code, s.sku, p.description, s.on_hand, s.reorder_point "
        "FROM stock_levels s JOIN products p ON p.sku = s.sku WHERE 1=1"
    )
    params: list = []
    if site_code:
        sql += " AND s.site_code = ?"
        params.append(site_code)
    if below_threshold:
        sql += " AND s.on_hand <= s.reorder_point"
    return db.query(sql + " ORDER BY s.site_code, s.sku", tuple(params))


def movement_history(db: Database, site_code: str, sku: str, limit: int = 50) -> list[dict]:
    return db.query(
        "SELECT id, movement_type, quantity_delta, balance_after, reference, created_at "
        "FROM ledger WHERE site_code = ? AND sku = ? ORDER BY id DESC LIMIT ?",
        (site_code, sku, limit),
    )
