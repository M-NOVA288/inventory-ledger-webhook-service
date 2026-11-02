"""REST API (standard library http.server, no framework needed).

Endpoints
  GET  /health
  GET  /inventory?site=HOU1&below_threshold=true
  GET  /inventory/{site}/{sku}
  GET  /inventory/{site}/{sku}/history
  POST /movements            {"site_code","sku","movement_type","quantity","reference"}
  PUT  /thresholds           {"site_code","sku","reorder_point"}
  GET  /alerts?status=pending
  POST /alerts/retry
"""

from __future__ import annotations

import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import alerts, ledger
from .db import Database
from .ledger import InventoryError


def make_handler(db: Database, webhook_url: str | None, webhook_secret: str | None):
    lock = threading.Lock()  # one shared connection; serialize DB access

    class Handler(BaseHTTPRequestHandler):
        server_version = "InventoryService/1.0"

        def log_message(self, fmt, *args):  # quieter default logging
            if os.environ.get("API_LOG"):
                super().log_message(fmt, *args)

        # ---- helpers -------------------------------------------------------
        def _send(self, status: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _json_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                raise InventoryError(f"invalid JSON: {exc.msg}") from exc
            if not isinstance(body, dict):
                raise InventoryError("request body must be a JSON object")
            return body

        @staticmethod
        def _require(body: dict, *fields: str) -> None:
            missing = [f for f in fields if body.get(f) in (None, "")]
            if missing:
                raise InventoryError(f"missing field(s): {', '.join(missing)}")

        def _route(self, method: str) -> None:
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                with lock:
                    status, body = self._dispatch(method, parts, query)
            except InventoryError as exc:
                status, body = HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)}
            except Exception as exc:  # last-resort guard so the server never dies
                status, body = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": type(exc).__name__}
            self._send(status, body)

        # ---- routes --------------------------------------------------------
        def _dispatch(self, method: str, parts: list[str], query: dict):
            if method == "GET" and parts == ["health"]:
                return HTTPStatus.OK, {"status": "ok"}

            if method == "GET" and parts == ["inventory"]:
                below = query.get("below_threshold", "").lower() in {"1", "true", "yes"}
                return HTTPStatus.OK, ledger.list_stock(db, query.get("site"), below)

            if method == "GET" and len(parts) == 3 and parts[0] == "inventory":
                row = ledger.get_stock(db, parts[1], parts[2])
                return (HTTPStatus.OK, row) if row else (HTTPStatus.NOT_FOUND, {"error": "not found"})

            if method == "GET" and len(parts) == 4 and parts[0] == "inventory" and parts[3] == "history":
                return HTTPStatus.OK, ledger.movement_history(db, parts[1], parts[2])

            if method == "POST" and parts == ["movements"]:
                body = self._json_body()
                self._require(body, "site_code", "sku", "movement_type", "quantity")
                result = ledger.record_movement(
                    db, body["site_code"], body["sku"], body["movement_type"],
                    body["quantity"], body.get("reference"),
                )
                if result["alert_id"]:
                    result["webhook"] = alerts.deliver_pending(db, webhook_url, webhook_secret)
                return HTTPStatus.CREATED, result

            if method == "PUT" and parts == ["thresholds"]:
                body = self._json_body()
                self._require(body, "site_code", "sku", "reorder_point")
                return HTTPStatus.OK, ledger.set_reorder_point(
                    db, body["site_code"], body["sku"], body["reorder_point"]
                )

            if method == "GET" and parts == ["alerts"]:
                sql = "SELECT * FROM alerts"
                params: tuple = ()
                if "status" in query:
                    sql, params = sql + " WHERE status = ?", (query["status"],)
                return HTTPStatus.OK, db.query(sql + " ORDER BY id DESC", params)

            if method == "POST" and parts == ["alerts", "retry"]:
                return HTTPStatus.OK, alerts.deliver_pending(db, webhook_url, webhook_secret)

            return HTTPStatus.NOT_FOUND, {"error": f"no route for {method} /{'/'.join(parts)}"}

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

    return Handler


def create_server(
    db: Database,
    host: str = "127.0.0.1",
    port: int = 8000,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> ThreadingHTTPServer:
    handler = make_handler(
        db,
        webhook_url or os.environ.get("ALERT_WEBHOOK_URL"),
        webhook_secret or os.environ.get("ALERT_WEBHOOK_SECRET"),
    )
    return ThreadingHTTPServer((host, port), handler)
