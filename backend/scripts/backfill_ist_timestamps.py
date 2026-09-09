"""One-time backfill: shift the columns Python used to write in UTC to IST.

WHY
---
Until 9 Sep 2026 this application had two time bases in the same database:

  * every `server_default=func.now()` column -- which is all 32 models' own
    `created_at`/`updated_at` -- was written by POSTGRES, whose timezone is
    Asia/Kolkata (db/initdb/01-timezone.sql), so those are **IST**;
  * a handful of columns filled from Python with `datetime.utcnow()` were
    **UTC**.

`dealer_portal_pushes` id=1 showed both in one row, written milliseconds
apart: created_at 13:57:45, completed_at 08:27:45.

The convention is now naive IST everywhere (see core/time_utils.py). The
writers were changed in the same commit; this script fixes the rows they
already wrote, by adding 5 hours 30 minutes to exactly those columns.

NOT TOUCHED
-----------
  * every `func.now()` column -- already IST, and by far the most data
    (~132,000 inventory rows alone). Shifting those is what this design
    deliberately avoids.
  * `revoked_tokens.expires_at` -- a JWT `exp` claim, genuinely UTC.

SAFETY
------
Runs inside ONE transaction and prints a before/after sample per column.
Dry-run by default; pass --apply to commit. Re-running would double-shift,
so it refuses unless --force is given as well.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from sqlalchemy import inspect, text

from core.db import engine

SHIFT = timedelta(hours=5, minutes=30)

# (table, column) pairs that Python wrote as UTC. Derived from the call sites
# changed in this commit -- `grep -rn "= _utcnow()\|= datetime.utcnow()"`.
COLUMNS: list[tuple[str, str]] = [
    ("incoming_documents", "processed_at"),
    ("inventory_imports", "completed_at"),
    ("inventory_imports", "confirmed_at"),
    ("customer_orders", "completed_at"),
    ("delivery_imports", "completed_at"),
    ("vendor_delivery_imports", "completed_at"),
    ("vendor_invoice_imports", "completed_at"),
    ("vendor_purchase_orders", "emailed_at"),
    ("gmail_integration_status", "last_poll_at"),
    ("gmail_integration_status", "last_message_processed_at"),
    ("google_sheets_integration_status", "last_sync_at"),
    ("whatsapp_integration_status", "last_webhook_verified_at"),
    ("whatsapp_integration_status", "last_connection_tested_at"),
    ("whatsapp_vendor_memory", "updated_at"),
    ("whatsapp_pending_commands", "updated_at"),
    ("dealer_portal_vendor_map", "asked_at"),
    ("dealer_portal_vendor_map", "confirmed_at"),
    ("dealer_portal_pushes", "completed_at"),
    ("learned_file_formats", "last_used_at"),
]

MARKER_TABLE = "ist_backfill_marker"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the change")
    parser.add_argument("--force", action="store_true", help="run again even if already applied")
    args = parser.parse_args()

    inspector = inspect(engine)
    present = set(inspector.get_table_names())

    with engine.begin() as conn:
        already = MARKER_TABLE in present
        if already and not args.force:
            print(f"REFUSING: {MARKER_TABLE} exists -- this backfill already ran.")
            print("Re-running would shift the same rows a SECOND time.")
            print("Pass --force only if you are certain that is what you want.")
            return 1

        total = 0
        for table, column in COLUMNS:
            if table not in present:
                print(f"  skip   {table}.{column:28s} (table not in this database)")
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column not in cols:
                print(f"  skip   {table}.{column:28s} (column not present)")
                continue

            n = conn.execute(
                text(f"select count(*) from {table} where {column} is not null")  # noqa: S608
            ).scalar_one()
            if not n:
                print(f"  empty  {table}.{column:28s} (0 rows)")
                continue

            sample = conn.execute(
                text(  # noqa: S608
                    f"select {column} from {table} where {column} is not null "
                    f"order by {column} desc limit 1"
                )
            ).scalar_one()
            print(f"  SHIFT  {table}.{column:28s} {n:6d} row(s)   "
                  f"{sample}  ->  {sample + SHIFT}")
            total += n

            if args.apply:
                conn.execute(
                    text(  # noqa: S608
                        f"update {table} set {column} = {column} + interval '5 hours 30 minutes' "
                        f"where {column} is not null"
                    )
                )

        print(f"\n  {total} row(s) across {len(COLUMNS)} candidate column(s)")

        if not args.apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply to commit.")
            return 0

        conn.execute(text(
            f"create table if not exists {MARKER_TABLE} "
            "(applied_at timestamp not null default now(), rows_shifted integer not null)"
        ))
        conn.execute(
            text(f"insert into {MARKER_TABLE} (rows_shifted) values (:n)"), {"n": total}
        )
        print("\nAPPLIED and recorded. Re-running is now refused without --force.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
