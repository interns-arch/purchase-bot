"""Check: a vendor's second stock file of the day is added below the first, on the
sheet and in the Portal push, and the first file of a new day replaces them.

Throwaway SQLite, never the real database, and no network call:

    .venv\\Scripts\\python.exe -m backend.scripts.check_same_day_append
"""

from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

_TMP = Path(tempfile.mkdtemp(prefix="same-day-append-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["AI_FALLBACK_ENABLED"] = "false"
os.environ["STOCK_APPEND_SAME_DAY_VENDORS"] = "*"
os.environ["STOCK_MULTI_FILE_VENDORS"] = "Spares 4U"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def stock(name: str, rows: list[tuple[str, int, int]]) -> Path:
    path = _TMP / name
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Part No", "Part Name", "Qty", "MRP"])
        for part, qty, mrp in rows:
            writer.writerow([part, f"Name {part}", qty, mrp])
    return path


def main() -> int:
    from sqlalchemy import select

    import backend.app.main  # noqa: F401 -- registers every table
    from backend.app.integrations.dealer_portal import delta
    from backend.app.integrations.google_sheets import dealer_stock
    from core.db import get_session, init_db
    from core.models import ImportStatus, InventoryImport, Vendor, VendorInventory
    from core.services import inventory_import_service as imp

    init_db(force=True)
    with get_session() as s:
        vendor = Vendor(name="Mohan Check", vendor_code="CT_CHK")
        s.add(vendor)
        s.flush()
        vendor_id = vendor.id

    def upload(name, rows):
        with get_session() as s:
            imp.run_import(vendor_id, stock(name, rows), s)

    def active_rows():
        with get_session() as s:
            active = s.execute(
                select(InventoryImport).where(InventoryImport.vendor_id == vendor_id, InventoryImport.is_active.is_(True))
            ).scalar_one()
            rows = s.execute(
                select(VendorInventory).where(VendorInventory.import_id == active.id).order_by(VendorInventory.row_number)
            ).scalars().all()
            return [(r.vendor_part_number, int(r.quantity_available)) for r in rows]

    account = SimpleNamespace(key="CHK", vendor_ids=[vendor_id], full_snapshot=True)

    def push():
        with get_session() as s:
            result = delta.build_delta(account, s)
        return {r.part_no: r.quantity for r in result.rows}, result

    print("\n[1] two files on one day")
    upload("morning.csv", [("AAA111", 5, 100), ("BBB222", 3, 200), ("CCC333", 2, 300)])
    upload("evening.csv", [("DDD444", 7, 400), ("BBB222", 1, 200)])
    rows = active_rows()
    check("every row of both files is kept", len(rows) == 5)
    check("the second file sits below the first", [r[0] for r in rows] == ["AAA111", "BBB222", "CCC333", "DDD444", "BBB222"])

    with get_session() as s:
        _, table = dealer_stock.all_stock_table(s)
    check("the sheet shows the same order", [r[2] for r in table] == ["AAA111", "BBB222", "CCC333", "DDD444", "BBB222"])

    sent, result = push()
    check("the Portal gets the parts of both files", set(sent) == {"AAA111", "BBB222", "CCC333", "DDD444"})
    check("nothing from the first file is zeroed", result.zeroed_count == 0 and sent["AAA111"] == 5)
    check("a part in both files goes at its higher quantity", sent["BBB222"] == 3 and result.duplicate_count == 1)

    print("\n[2] the same file again")
    upload("evening.csv", [("DDD444", 7, 400), ("BBB222", 1, 200)])
    check("adds no second copy", len(active_rows()) == 5)
    with get_session() as s:  # the worker discards such a repeat the same way
        waiting = s.execute(select(InventoryImport).where(InventoryImport.status == ImportStatus.AWAITING_CONFIRMATION)).scalar_one()
        imp.cancel_import(waiting.id, s)

    print("\n[3] the next day")
    with get_session() as s:
        for earlier in s.execute(select(InventoryImport).where(InventoryImport.vendor_id == vendor_id)).scalars():
            earlier.created_at = (earlier.created_at or datetime.now()) - timedelta(days=1)
            if earlier.completed_at:
                earlier.completed_at = earlier.completed_at - timedelta(days=1)
    upload("nextday.csv", [("AAA111", 9, 100)])
    check("the first file of a new day replaces the old stock", active_rows() == [("AAA111", 9)])
    sent, result = push()
    check("and what is gone is zeroed on the Portal", sent["CCC333"] == 0 and sent["AAA111"] == 9 and result.zeroed_count == 3)

    print("\n[4] the setting")
    os.environ["STOCK_APPEND_SAME_DAY_VENDORS"] = ""
    with get_session() as s:
        check("empty: nobody appends", not imp._append_same_day_vendor(vendor_id, s))
    os.environ["STOCK_APPEND_SAME_DAY_VENDORS"] = "CT_BS"
    with get_session() as s:
        check("a list: only those vendors", not imp._append_same_day_vendor(vendor_id, s))
    os.environ["STOCK_APPEND_SAME_DAY_VENDORS"] = "*"
    with get_session() as s:
        check("*: every vendor", imp._append_same_day_vendor(vendor_id, s))

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
