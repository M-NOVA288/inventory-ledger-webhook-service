"""Mock ERP connector: replays ERP transactions into the inventory service over REST.

In production this would page through an ERP's API (SAP, NetSuite, Oracle);
here it reads an exported JSON file so the demo runs offline.

    python erp_sync.py mock_erp/transactions.json --api http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

# ERP transaction codes mapped to this service's movement types.
ERP_TYPE_MAP = {"GR": "receipt", "GI": "shipment", "ADJ": "adjustment"}


def to_movement(txn: dict) -> dict:
    if txn["txn_type"] not in ERP_TYPE_MAP:
        raise ValueError(f"unknown ERP txn_type {txn['txn_type']!r}")
    return {
        "site_code": txn["plant"],
        "sku": txn["material"],
        "movement_type": ERP_TYPE_MAP[txn["txn_type"]],
        "quantity": int(txn["qty"]),
        "reference": txn.get("doc_number"),
    }


def post(api: str, path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{api.rstrip('/')}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def sync(transactions: list[dict], api: str) -> dict:
    results = {"posted": 0, "rejected": [], "alerts_raised": []}
    for txn in transactions:
        try:
            movement = to_movement(txn)
        except (KeyError, ValueError) as exc:
            results["rejected"].append({"doc": txn.get("doc_number"), "error": str(exc)})
            continue
        status, body = post(api, "/movements", movement)
        if status == 201:
            results["posted"] += 1
            if body.get("alert_id"):
                results["alerts_raised"].append(f"{body['sku']}@{body['site_code']} -> {body['on_hand']}")
        else:
            results["rejected"].append({"doc": txn.get("doc_number"), "error": body.get("error")})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    results = sync(json.loads(args.file.read_text()), args.api)
    print(json.dumps(results, indent=2))
    return 0 if not results["rejected"] else 1


if __name__ == "__main__":
    sys.exit(main())
