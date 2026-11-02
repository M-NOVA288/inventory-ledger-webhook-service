"""Webhook delivery for low-stock alerts.

Alerts are written to the database first (an outbox), then delivered. If the
receiver is down, the alert stays 'pending' and is retried later, so nothing
is lost. Each request is signed with HMAC-SHA256 so the receiver (n8n, Power
Automate, a Slack relay) can verify it came from this service.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

from .db import Database

MAX_ATTEMPTS = 5


def build_payload(alert: dict) -> dict:
    shortfall = alert["reorder_point"] - alert["on_hand"]
    return {
        "event": "inventory.below_threshold",
        "alert_id": alert["id"],
        "site_code": alert["site_code"],
        "sku": alert["sku"],
        "on_hand": alert["on_hand"],
        "reorder_point": alert["reorder_point"],
        "severity": "critical" if alert["on_hand"] == 0 else "warning",
        "message": (
            f"{alert['sku']} at {alert['site_code']} is at {alert['on_hand']} "
            f"(reorder point {alert['reorder_point']}, short {max(shortfall, 0)})"
        ),
    }


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send_webhook(url: str, payload: dict, secret: str | None = None, timeout: int = 5) -> None:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "inventory-webhook-service/1.0"}
    if secret:
        headers["X-Signature-256"] = sign(body, secret)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status >= 300:
            raise urllib.error.HTTPError(url, response.status, "non-2xx response", None, None)


def deliver_pending(
    db: Database,
    url: str | None = None,
    secret: str | None = None,
    backoff_seconds: float = 0.0,
) -> dict:
    """Try to send every pending alert. Returns counts of sent and failed."""
    url = url or os.environ.get("ALERT_WEBHOOK_URL")
    secret = secret if secret is not None else os.environ.get("ALERT_WEBHOOK_SECRET")
    if not url:
        return {"sent": 0, "failed": 0, "skipped": "ALERT_WEBHOOK_URL not set"}

    pending = db.query(
        "SELECT id, site_code, sku, on_hand, reorder_point, attempts FROM alerts "
        "WHERE status = 'pending' ORDER BY id"
    )
    sent = failed = 0
    for alert in pending:
        try:
            send_webhook(url, build_payload(alert), secret)
            db.execute(
                "UPDATE alerts SET status = 'sent', attempts = attempts + 1, last_error = NULL WHERE id = ?",
                (alert["id"],),
            )
            sent += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            attempts = alert["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            db.execute(
                "UPDATE alerts SET status = ?, attempts = ?, last_error = ? WHERE id = ?",
                (status, attempts, str(exc)[:500], alert["id"]),
            )
            failed += 1
            if backoff_seconds:
                time.sleep(backoff_seconds * attempts)
        db.commit()
    return {"sent": sent, "failed": failed}
