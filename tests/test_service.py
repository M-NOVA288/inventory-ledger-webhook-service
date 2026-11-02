import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from erp_sync import sync, to_movement
from inventory_service import alerts, ledger
from inventory_service.api import create_server
from inventory_service.db import Database
from inventory_service.ledger import InventoryError
from inventory_service.seed import seed


# ---------------------------------------------------------------- fixtures --
@pytest.fixture
def db():
    database = Database("sqlite:///:memory:")
    database.init_schema()
    seed(database)
    yield database
    database.close()


class WebhookReceiver:
    """A tiny local HTTP server that records every webhook it receives."""

    def __init__(self, fail: bool = False):
        received, self.fail = [], fail
        self.received = received
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append({"body": json.loads(body), "signature": self.headers.get("X-Signature-256"),
                                 "raw": body})
                self.send_response(500 if outer.fail else 200)
                self.end_headers()

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver():
    r = WebhookReceiver()
    yield r
    r.close()


@pytest.fixture
def api(db, receiver):
    server = create_server(db, port=0, webhook_url=receiver.url, webhook_secret="s3cret")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


# ------------------------------------------------------------ ledger rules --
def test_receipt_and_shipment_update_balance_and_ledger(db):
    ledger.record_movement(db, "HOU1", "PC-10432", "receipt", 20, "PO-1")
    result = ledger.record_movement(db, "HOU1", "PC-10432", "shipment", 100, "SO-1")
    assert result["on_hand"] == 480 + 20 - 100
    history = ledger.movement_history(db, "HOU1", "PC-10432")
    assert [h["quantity_delta"] for h in history] == [-100, 20]
    assert history[0]["balance_after"] == 400


def test_cannot_ship_more_than_on_hand(db):
    with pytest.raises(InventoryError, match="insufficient stock"):
        ledger.record_movement(db, "LRD1", "RG-4410", "shipment", 19)
    assert ledger.get_stock(db, "LRD1", "RG-4410")["on_hand"] == 18  # unchanged


@pytest.mark.parametrize(
    "mtype, qty, message",
    [("transfer", 5, "movement_type"), ("receipt", 0, "must be positive"),
     ("shipment", -3, "must be positive"), ("adjustment", 0, "cannot be zero"),
     ("receipt", 2.5, "integer")],
)
def test_invalid_movements_rejected(db, mtype, qty, message):
    with pytest.raises(InventoryError, match=message):
        ledger.record_movement(db, "HOU1", "PC-10432", mtype, qty)


def test_unknown_site_or_sku(db):
    with pytest.raises(InventoryError, match="no stock record"):
        ledger.record_movement(db, "HOU1", "NOPE-1", "receipt", 1)


def test_alert_raised_only_on_threshold_crossing(db):
    # HOU1 PC-20981: on hand 60, reorder point 25
    first = ledger.record_movement(db, "HOU1", "PC-20981", "shipment", 30)   # 30, still above
    second = ledger.record_movement(db, "HOU1", "PC-20981", "shipment", 10)  # 20, crosses
    third = ledger.record_movement(db, "HOU1", "PC-20981", "shipment", 5)    # 15, already low
    assert first["alert_id"] is None
    assert second["alert_id"] is not None
    assert third["alert_id"] is None
    assert len(db.query("SELECT * FROM alerts")) == 1


def test_below_threshold_filter(db):
    ledger.record_movement(db, "DAL1", "PC-30112", "shipment", 30)  # 90 <= 100
    low = ledger.list_stock(db, below_threshold=True)
    assert [(r["site_code"], r["sku"]) for r in low] == [("DAL1", "PC-30112")]


def test_composite_index_used_for_site_sku_queries(db):
    plan = db.query(
        "EXPLAIN QUERY PLAN SELECT * FROM ledger WHERE site_code = ? AND sku = ? AND created_at > ?",
        ("HOU1", "PC-10432", "2026-01-01"),
    )
    assert "idx_ledger_site_sku_time" in plan[0]["detail"]


# ---------------------------------------------------------------- webhooks --
def test_webhook_payload_and_signature(db, receiver):
    ledger.record_movement(db, "LRD1", "RG-4410", "shipment", 18)  # to zero: critical
    assert alerts.deliver_pending(db, receiver.url, "s3cret") == {"sent": 1, "failed": 0}
    hook = receiver.received[0]
    assert hook["body"]["event"] == "inventory.below_threshold"
    assert hook["body"]["severity"] == "critical"
    assert hook["signature"] == alerts.sign(hook["raw"], "s3cret")
    assert db.query("SELECT status FROM alerts")[0]["status"] == "sent"


def test_failed_delivery_stays_pending_then_gives_up(db):
    down = WebhookReceiver(fail=True)
    try:
        ledger.record_movement(db, "HOU1", "PC-20981", "shipment", 40)
        for attempt in range(1, alerts.MAX_ATTEMPTS + 1):
            alerts.deliver_pending(db, down.url)
            row = db.query("SELECT status, attempts FROM alerts")[0]
            assert row["attempts"] == attempt
        assert row["status"] == "failed"
    finally:
        down.close()


def test_no_webhook_url_is_a_noop(db, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert alerts.deliver_pending(db)["sent"] == 0


# --------------------------------------------------------------------- API --
def test_health(api):
    assert call("GET", f"{api}/health") == (200, {"status": "ok"})


def test_api_movement_triggers_webhook(api, receiver):
    status, body = call("POST", f"{api}/movements", {
        "site_code": "HOU1", "sku": "PC-20981", "movement_type": "shipment", "quantity": 40,
        "reference": "SO-9001"})
    assert status == 201
    assert body["below_threshold"] is True
    assert body["webhook"] == {"sent": 1, "failed": 0}
    assert receiver.received[0]["body"]["sku"] == "PC-20981"


def test_api_validation_errors(api):
    status, body = call("POST", f"{api}/movements", {"site_code": "HOU1"})
    assert status == 422 and "missing field" in body["error"]
    status, body = call("POST", f"{api}/movements", {
        "site_code": "HOU1", "sku": "PC-10432", "movement_type": "shipment", "quantity": 99999})
    assert status == 422 and "insufficient stock" in body["error"]


def test_api_thresholds_and_lookup(api):
    status, body = call("PUT", f"{api}/thresholds", {"site_code": "DAL1", "sku": "RG-4410", "reorder_point": 40})
    assert status == 200 and body["reorder_point"] == 40
    status, rows = call("GET", f"{api}/inventory?site=DAL1&below_threshold=true")
    assert {r["sku"] for r in rows} == {"RG-4410"}
    assert call("GET", f"{api}/inventory/DAL1/NOPE")[0] == 404


def test_api_unknown_route(api):
    assert call("GET", f"{api}/nope")[0] == 404


# ---------------------------------------------------------------- ERP sync --
def test_erp_mapping():
    assert to_movement({"txn_type": "GI", "plant": "HOU1", "material": "X-1", "qty": "5",
                        "doc_number": "D1"})["movement_type"] == "shipment"
    with pytest.raises(ValueError):
        to_movement({"txn_type": "XFER", "plant": "HOU1", "material": "X-1", "qty": 1})


def test_erp_sync_end_to_end(api, receiver):
    with open("mock_erp/transactions.json") as f:
        result = sync(json.load(f), api)
    assert result["posted"] == 5
    assert len(result["rejected"]) == 2          # overdraw + unknown txn type
    assert result["alerts_raised"] == [
        "PC-20981@HOU1 -> 22",   # 60 - 20 - 18, reorder point 25
        "PC-30112@DAL1 -> 90",   # 120 - 30, reorder point 100
        "RG-4410@LRD1 -> 12",    # 18 - 6, reorder point 12
    ]
    assert len(receiver.received) == 3
