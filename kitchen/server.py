"""Fake payments MCP server (stdio). Stores charges and emails in SQLite.

Run directly:  python kitchen/server.py
The DB path defaults to data/payments.db; override with DOUBLETAKE_DB.

Fault injection (Phase 1)
-------------------------
Faults are read from the JSON file named by DOUBLETAKE_FAULTS (default
kitchen/faults.json) on EVERY tool call, so a running server picks up edits.

    {
      "lose_reply":   {"enabled": true, "tools": ["charge_card"], "hang_seconds": 30, "times": 1},
      "pending":      {"enabled": true, "pending_seconds": 30, "times": 1},
      "lookup_fails": {"enabled": true, "hang_seconds": 30, "times": 1}
    }

lose_reply    charge_card / send_email COMMIT the row, then sleep hang_seconds
              before replying, so the caller times out. The row stays committed
              even if the client cancels the request during the sleep.
pending       list_charges hides charges younger than pending_seconds.
lookup_fails  list_charges sleeps hang_seconds before answering.

"times": N applies the fault to the first N matching calls, then the tool
behaves normally. Omit "times" (or set null) to apply it to every call. The
counters live in this process's memory and reset when the server restarts.

Tools are async and hang with asyncio.sleep, so while one call is hanging the
server keeps serving other requests.
"""

import asyncio
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "payments.db"
DEFAULT_FAULTS = ROOT / "kitchen" / "faults.json"
DEFAULT_HANG_SECONDS = 30.0
DEFAULT_PENDING_SECONDS = 30.0


def db_path() -> Path:
    return Path(os.environ.get("DOUBLETAKE_DB", DEFAULT_DB))


def faults_path() -> Path:
    return Path(os.environ.get("DOUBLETAKE_FAULTS", DEFAULT_FAULTS))


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
               created_at   TEXT NOT NULL,
               note         TEXT NOT NULL DEFAULT ''
           )"""
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(charges)")}
    if "note" not in cols:  # databases created before Phase 2
        conn.execute("ALTER TABLE charges ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS emails (
               email_id   TEXT PRIMARY KEY,
               to_addr    TEXT NOT NULL,
               subject    TEXT NOT NULL,
               body       TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    return conn


def insert(sql: str, params: tuple) -> None:
    """Insert and COMMIT before returning; nothing after this can roll it back."""
    conn = connect()
    try:
        with conn:  # commits on exit
            conn.execute(sql, params)
    finally:
        conn.close()


# ---------------------------------------------------------------- faults

_fault_counts: dict[str, int] = {}  # fault name -> times applied in this process


def reset_fault_counters() -> None:
    _fault_counts.clear()


def load_faults() -> dict:
    path = faults_path()
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def take_fault(name: str, tool: str | None = None) -> dict | None:
    """Return the fault's config if it applies to this call (and count it), else None."""
    cfg = load_faults().get(name)
    if not cfg or not cfg.get("enabled", False):
        return None
    if tool is not None and tool not in cfg.get("tools", []):
        return None
    times = cfg.get("times")
    used = _fault_counts.get(name, 0)
    if times is not None and used >= times:
        return None
    _fault_counts[name] = used + 1
    return cfg


async def maybe_lose_reply(tool: str) -> None:
    """Called AFTER the row is committed: hang so the reply arrives too late."""
    cfg = take_fault("lose_reply", tool)
    if cfg:
        await asyncio.sleep(float(cfg.get("hang_seconds", DEFAULT_HANG_SECONDS)))


# ---------------------------------------------------------------- tools

server = MCPServer("doubletake-payments")


def now() -> datetime:
    return datetime.now(timezone.utc)


@server.tool(
    description="Charge a customer's card. Amount is in dollars (e.g. 50.00). Optional note is stored with the charge.",
    annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
    structured_output=False,
)
async def charge_card(customer: str, amount: float, note: str = "") -> str:
    charge_id = "ch_" + secrets.token_hex(8)
    amount_cents = round(amount * 100)
    created_at = now().isoformat()
    insert(
        "INSERT INTO charges (charge_id, customer, amount_cents, created_at, note) VALUES (?, ?, ?, ?, ?)",
        (charge_id, customer, amount_cents, created_at, note),
    )
    await maybe_lose_reply("charge_card")
    return json.dumps(
        {
            "charge_id": charge_id,
            "status": "succeeded",
            "customer": customer,
            "amount_cents": amount_cents,
            "created_at": created_at,
            "note": note,
        }
    )


@server.tool(
    description="List a customer's charges, newest first.",
    annotations=ToolAnnotations(read_only_hint=True),
    structured_output=False,
)
async def list_charges(customer: str) -> str:
    hang = take_fault("lookup_fails")
    if hang:
        await asyncio.sleep(float(hang.get("hang_seconds", DEFAULT_HANG_SECONDS)))

    conn = connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT charge_id, customer, amount_cents, created_at, note FROM charges "
                "WHERE customer = ? ORDER BY created_at DESC, rowid DESC",
                (customer,),
            ).fetchall()
        ]
    finally:
        conn.close()

    pending = take_fault("pending")
    if pending:
        cutoff = now() - timedelta(seconds=float(pending.get("pending_seconds", DEFAULT_PENDING_SECONDS)))
        rows = [r for r in rows if datetime.fromisoformat(r["created_at"]) <= cutoff]

    return json.dumps({"customer": customer, "charges": rows})


@server.tool(
    description="Send an email.",
    annotations=ToolAnnotations(read_only_hint=False, idempotent_hint=False),
    structured_output=False,
)
async def send_email(to: str, subject: str, body: str) -> str:
    email_id = "em_" + secrets.token_hex(8)
    created_at = now().isoformat()
    insert(
        "INSERT INTO emails (email_id, to_addr, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
        (email_id, to, subject, body, created_at),
    )
    await maybe_lose_reply("send_email")
    return json.dumps({"email_id": email_id, "status": "sent", "to": to, "created_at": created_at})


if __name__ == "__main__":
    server.run()  # stdio by default
