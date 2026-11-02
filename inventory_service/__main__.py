"""Start the service:  python -m inventory_service --seed --port 8000"""

from __future__ import annotations

import argparse

from .api import create_server
from .db import Database
from .seed import seed


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory ledger and webhook alert service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="database URL (default: sqlite:///inventory.db)")
    parser.add_argument("--seed", action="store_true", help="load demo sites, products and stock")
    args = parser.parse_args()

    db = Database(args.db)
    db.init_schema()
    if args.seed:
        seed(db)

    server = create_server(db, args.host, args.port)
    print(f"Inventory service listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        db.close()


if __name__ == "__main__":
    main()
