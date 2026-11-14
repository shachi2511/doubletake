"""Print the charges and emails tables.

Usage: python scripts/show_charges.py [--emails | --charges] [db_path]
Default db: $DOUBLETAKE_DB or data/payments.db. Prints both tables unless one is chosen.
"""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def print_table(header, rows) -> None:
    table = [header] + rows
    widths = [max(len(str(r[i])) for r in table) for i in range(len(header))]
    for i, r in enumerate(table):
        print("  ".join(str(v).ljust(w) for v, w in zip(r, widths)))
        if i == 0:
            print("  ".join("-" * w for w in widths))
    print(f"\n{len(rows)} row(s)")


def table_exists(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def show_charges(conn) -> None:
    print("CHARGES")
    rows = []
    if table_exists(conn, "charges"):
        rows = [
            (cid, cust, f"${cents / 100:.2f}", ts)
            for cid, cust, cents, ts in conn.execute(
                "SELECT charge_id, customer, amount_cents, created_at FROM charges ORDER BY created_at, rowid"
            )
        ]
    print_table(("charge_id", "customer", "amount", "created_at"), rows)


def show_emails(conn) -> None:
    print("EMAILS")
    rows = []
    if table_exists(conn, "emails"):
        rows = [
            (eid, to, subj, body if len(body) <= 60 else body[:57] + "...", ts)
            for eid, to, subj, body, ts in conn.execute(
                "SELECT email_id, to_addr, subject, replace(body, char(10), ' '), created_at "
                "FROM emails ORDER BY created_at, rowid"
            )
        ]
    print_table(("email_id", "to", "subject", "body", "created_at"), rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--emails", action="store_true", help="only the emails table")
    g.add_argument("--charges", action="store_true", help="only the charges table")
    ap.add_argument("db", nargs="?", default=os.environ.get("DOUBLETAKE_DB", ROOT / "data" / "payments.db"))
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"No database yet at {db} (nothing has been written).")
        return 0
    conn = sqlite3.connect(db)
    if not args.emails:
        show_charges(conn)
    if not args.emails and not args.charges:
        print()
    if not args.charges:
        show_emails(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
