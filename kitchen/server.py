"""Fake payments MCP server (stdio). Stores charges in SQLite.

Run directly:  python kitchen/server.py
The DB path defaults to data/payments.db; override with DOUBLETAKE_DB.
"""

import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "payments.db"


def db_path() -> Path:
    return Path(os.environ.get("DOUBLETAKE_DB", DEFAULT_DB))


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS charges (
               charge_id    TEXT PRIMARY KEY,
               customer     TEXT NOT NULL,
               amount_cents INTEGER NOT NULL,
               created_at   TEXT NOT NULL
           )"""
    )
    return conn


server = MCPServer("doubletake-payments")


@server.tool(
    description="Charge a customer's card. Amount is in dollars (e.g. 50.00).",
    annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
    structured_output=False,
)
def charge_card(customer: str, amount: float) -> str:
    charge_id = "ch_" + secrets.token_hex(8)
    amount_cents = round(amount * 100)
    created_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO charges (charge_id, customer, amount_cents, created_at) VALUES (?, ?, ?, ?)",
            (charge_id, customer, amount_cents, created_at),
        )
    return json.dumps(
        {
            "charge_id": charge_id,
            "status": "succeeded",
            "customer": customer,
            "amount_cents": amount_cents,
            "created_at": created_at,
        }
    )


@server.tool(
    description="List a customer's charges, newest first.",
    annotations=ToolAnnotations(read_only_hint=True),
    structured_output=False,
)
def list_charges(customer: str) -> str:
    with connect() as conn:
        rows = conn.execute(
            "SELECT charge_id, customer, amount_cents, created_at FROM charges "
            "WHERE customer = ? ORDER BY created_at DESC, rowid DESC",
            (customer,),
        ).fetchall()
    return json.dumps({"customer": customer, "charges": [dict(r) for r in rows]})


if __name__ == "__main__":
    server.run()  # stdio by default
