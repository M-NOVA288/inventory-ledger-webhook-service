# Enterprise Inventory Ledger & Webhook Service

A multi-site inventory ledger exposed over a REST API. It syncs transactions from a (mock) ERP and fires signed webhooks the moment a stock count drops to its safety threshold. The webhooks can feed n8n, Power Automate, or Slack.

**Stack:** Python 3.10+ (standard library only), SQLite or PostgreSQL, REST, webhooks (HMAC-signed), n8n, Docker, pytest.

## What it does

```
 ERP export ──► erp_sync.py ──REST──► Inventory API ──► stock_levels + ledger (append-only)
                                            │
                                 threshold crossed?
                                            │
                                     alerts (outbox) ──► signed webhook ──► n8n
                                                                            ├─ critical → #supply-chain-urgent
                                                                            ├─ warning  → #inventory-alerts
                                                                            └─ log row  → Google Sheet
```

- **Ledger, not just counts.** Every movement (receipt, shipment, adjustment) is an append-only ledger row with the balance after it, so any number can be traced to the document that caused it.
- **Business rules enforced in one place.** No negative stock, no zero or fractional quantities, and no movements against unknown site/SKU pairs. Each movement's balance update, ledger row, and alert commit together or not at all.
- **Alerts fire on the crossing, not on every low reading.** A SKU that is already low doesn't spam the channel on each subsequent shipment.
- **No lost alerts.** Alerts are written to an outbox table before delivery. If the receiver is down they stay `pending` and are retried (`POST /alerts/retry`), and are marked `failed` after 5 attempts.
- **Signed webhooks.** Each request carries `X-Signature-256: sha256=<hmac>` so the receiver can verify the sender.

## Run it

```bash
python -m inventory_service --seed            # SQLite, http://127.0.0.1:8000
python erp_sync.py mock_erp/transactions.json # in a second terminal
```

Real output from the mock ERP replay:

```json
{
  "posted": 5,
  "rejected": [
    {"doc": "4900012304", "error": "insufficient stock at LRD1 for PC-20981: on hand 40, requested 500"},
    {"doc": "4900012305", "error": "unknown ERP txn_type 'XFER'"}
  ],
  "alerts_raised": ["PC-20981@HOU1 -> 22", "PC-30112@DAL1 -> 90", "RG-4410@LRD1 -> 12"]
}
```

To deliver alerts somewhere, set `ALERT_WEBHOOK_URL` (and optionally `ALERT_WEBHOOK_SECRET`) before starting the service.

### With PostgreSQL and n8n

```bash
docker compose up --build
```

Then open n8n at http://localhost:5678, import `n8n/low_stock_alert_workflow.json`, connect your Slack and Google Sheets credentials, and activate the workflow.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| GET | `/inventory?site=HOU1&below_threshold=true` | Stock levels, filterable |
| GET | `/inventory/{site}/{sku}` | One stock record |
| GET | `/inventory/{site}/{sku}/history` | Ledger history, newest first |
| POST | `/movements` | `{site_code, sku, movement_type, quantity, reference}` |
| PUT | `/thresholds` | `{site_code, sku, reorder_point}` |
| GET | `/alerts?status=pending` | Alert outbox |
| POST | `/alerts/retry` | Re-send pending alerts |

Rule violations return `422` with a plain-English `error`.

Webhook payload:

```json
{
  "event": "inventory.below_threshold",
  "alert_id": 1,
  "site_code": "LRD1",
  "sku": "RG-4410",
  "on_hand": 0,
  "reorder_point": 12,
  "severity": "critical",
  "message": "RG-4410 at LRD1 is at 0 (reorder point 12, short 12)"
}
```

## Indexing

The most common query filters the ledger by site, SKU, and date range. A composite index on `(site_code, sku, created_at)` turns that from a full table scan into an index search. `bench_indexes.py` measures it:

```
        no index: median 13.365 ms   plan: SCAN ledger
 composite index: median 0.043 ms   plan: SEARCH ledger USING INDEX idx_ledger_site_sku_time (...)

300,000 ledger rows, 200 queries each: 312x faster with the index
```

These numbers come from an in-memory SQLite run, so exact timings vary by machine. The Postgres schema also adds a partial index for the "what's below threshold right now" dashboard query.

## Tests

```bash
pip install pytest
python -m pytest -q
```

The 21 tests cover ledger rules, threshold-crossing logic, index usage, webhook signing, retry/give-up behavior, every API route, and the full ERP sync. The test suite runs on SQLite. The PostgreSQL path uses the same SQL through `schema/postgres.sql` and the `psycopg` driver.

## Layout

```
inventory_service/
  db.py        SQLite/Postgres connection wrapper and schema
  ledger.py    movement rules, balances, threshold detection
  alerts.py    outbox delivery, HMAC signing, retries
  api.py       REST endpoints
  seed.py      demo sites, products, stock
erp_sync.py    mock ERP connector
bench_indexes.py
n8n/           importable alert-routing workflow
schema/        PostgreSQL DDL
```
