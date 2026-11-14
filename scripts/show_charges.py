"""Print the charges table.  Usage: python scripts/show_charges.py"""

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = Path(os.environ.get("DOUBLETAKE_DB", ROOT / "data" / "payments.db"))

if not DB.exists():
    print(f"No database yet at {DB} (no charges have been made).")
    sys.exit(0)

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT charge_id, customer, amount_cents, created_at FROM charges ORDER BY created_at, rowid"
).fetchall()

header = ("charge_id", "customer", "amount", "created_at")
table = [header] + [(cid, cust, f"${cents / 100:.2f}", ts) for cid, cust, cents, ts in rows]
widths = [max(len(str(r[i])) for r in table) for i in range(4)]
for i, r in enumerate(table):
    print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))
    if i == 0:
        print("  ".join("-" * w for w in widths))
print(f"\n{len(rows)} row(s)")
