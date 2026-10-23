"""Check: for a vendor in STOCK_APPEND_SAME_DAY_VENDORS (CT_BS, whose stock
comes from two people) a second file on the same day is added BELOW the
first; a file on a new day still replaces yesterday's (20 Oct 2026).

Throwaway SQLite, never the real database:

    .venv\\Scripts\\python.exe -m backend.scripts.check_append_same_day_stock
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="append-same-day-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["STOCK_APPEND_SAME_DAY_VENDORS"] = "CT_BS"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def stock_file(name: str, rows: list[tuple]) -> Path:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["PART NO", "PART NAME", "QTY"])
    for r in rows:
        ws.append(list(r))
    path = _TMP / name
    wb.save(path)
    return path


def main() -> int:
    from sqlalchemy import select

    from core.db import get_session, init_db
    from core.models import InventoryImport, Vendor
    from core.services import inventory_import_service as svc

    init_db(force=True)
    first = stock_file("vendor_stock.xlsx", [("AAA111", "FILTER", 2), ("BBB222", "PAD", 1)])
    second = stock_file("srawan_stock.xlsx", [("CCC333", "CLIP", 5), ("DDD444", "BULB", 3)])
    third = stock_file("srawan_stock_2.xlsx", [("EEE555", "BELT", 1)])

    with get_session() as s:
        bs = Vendor(name="BS Motors", vendor_code="CT_BS")
        other = Vendor(name="Other Motors", vendor_code="CT_OM")
        s.add_all([bs, other])
        s.flush()
        bid, oid = bs.id, other.id

    def active_parts(vendor_id):
        with get_session() as s:
            return [r.normalized_part_number for r in svc.get_active_inventory(vendor_id, s)]

    print("\n[1] CT_BS: second file the same day goes BELOW the first")
    with get_session() as s:
        svc.run_import(bid, first, s)
    with get_session() as s:
        r = svc.run_import(bid, second, s)
    parts = active_parts(bid)
    check("first file's rows on top, new rows below", parts == ["AAA111", "BBB222", "CCC333", "DDD444"])
    check("row count covers both files", r.row_count == 4)

    print("\n[2] a third file the same day is added below both")
    with get_session() as s:
        svc.run_import(bid, third, s)
    check("all three files, in order", active_parts(bid) == ["AAA111", "BBB222", "CCC333", "DDD444", "EEE555"])

    print("\n[2b] the Google Sheet tab (exact copy) shows today's files one below the other")
    from backend.app.documents.models import DocumentSource, IncomingDocument
    from backend.app.integrations.google_sheets.sync_service import original_file_grid

    with get_session() as s:
        for imp in s.execute(
            select(InventoryImport).where(InventoryImport.vendor_id == bid).order_by(InventoryImport.id)
        ).scalars():
            s.add(IncomingDocument(
                source=DocumentSource.WHATSAPP, filename=imp.file_name,
                stored_path=str(_TMP / imp.file_name), inventory_import_id=imp.id,
            ))
    with get_session() as s:
        grid = original_file_grid(bid, s)
    check("one header, then all 5 rows in order", grid is not None and [r[0] for r in grid] == [
        "PART NO", "AAA111", "BBB222", "CCC333", "DDD444", "EEE555"])

    print("\n[3] next day: the first file replaces yesterday's stock")
    with get_session() as s:
        active = s.execute(
            select(InventoryImport).where(InventoryImport.vendor_id == bid, InventoryImport.is_active.is_(True))
        ).scalar_one()
        active.completed_at = active.completed_at - timedelta(days=1)
    with get_session() as s:
        svc.run_import(bid, second, s)
    check("only the new day's file", active_parts(bid) == ["CCC333", "DDD444"])

    print("\n[4] a normal vendor is unchanged: a new file replaces the old one")
    with get_session() as s:
        svc.run_import(oid, first, s)
    with get_session() as s:
        svc.run_import(oid, second, s)
    check("Other Motors: only the latest file", active_parts(oid) == ["CCC333", "DDD444"])

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
