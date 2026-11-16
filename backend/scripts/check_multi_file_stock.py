"""Check: a vendor's workbook split by brand is read whole, and a vendor who
sends SEVERAL stock files keeps all of them (Founder, 12-13 Oct 2026).

Throwaway SQLite, never the real database:

    .venv\\Scripts\\python.exe -m backend.scripts.check_multi_file_stock
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="multi-file-stock-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["STOCK_MULTI_FILE_VENDORS"] = "Spares 4U"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def workbook(path: Path, sheets: dict[str, list[tuple]]) -> Path:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for r in rows:
            ws.append(list(r))
    wb.save(path)
    return path


def main() -> int:
    from sqlalchemy import select

    from core.db import get_session, init_db
    from core.models import InventoryImport, Vendor, VendorInventory
    from core.services import inventory_import_service as svc

    init_db(force=True)
    head = ("S.NO", "PART NO", "PART NAME", "QTY", "MRP", "TOTAL")
    brands = workbook(_TMP / "STOCK_HYUNDAI_TOYOTA_VW_HONDA.xlsx", {
        "HYUNDAI": [head, (1, "95910B4000", "AIR BAG", 1, 9753, 9753), (2, "71504B4C10", "PANEL", 1, 7175, 7175)],
        "VW": [head, (1, "5Q0698151", "PAD", 2, 3000, 6000)],
        "HONDA": [("S.NO", "PART NO", "PART NAME", "QTY", "MRP"), (1, "08CZ1M00000", "OIL", 4, 500)],
        "NOTES": [("remark",), ("call before pickup",)],
    })
    renault = workbook(_TMP / "STOCK_RENAULT_AND_NISSAN.xlsx", {
        "Sheet1": [("S.NO", "PART NUMBER", "PART NAME", "QTY", "MRP", "TOTAL"),
                   (1, "7703083335", "CLIP", 1, 89, 89), (2, "7703077210", "CLIP 2", 3, 43, 129)],
    })

    with get_session() as s:
        v = Vendor(name="Spares 4U", vendor_code="CT_S4")
        other = Vendor(name="Other Motors", vendor_code="CT_OM")
        s.add_all([v, other])
        s.flush()
        vid, oid = v.id, other.id

    def active_parts(vendor_id):
        with get_session() as s:
            imp = s.execute(select(InventoryImport).where(InventoryImport.vendor_id == vendor_id, InventoryImport.is_active.is_(True))).scalar_one()
            rows = s.execute(select(VendorInventory).where(VendorInventory.import_id == imp.id)).scalars().all()
            return imp, {r.normalized_part_number: (int(r.quantity_available), r.mrp) for r in rows}

    print("\n[1] a workbook with one sheet per brand is read whole")
    with get_session() as s:
        svc.run_import(vid, renault, s)
    with get_session() as s:
        svc.run_import(vid, brands, s)
    imp, parts = active_parts(vid)
    check("HYUNDAI + VW + HONDA sheets all read (4 parts)", {"95910B4000", "71504B4C10", "5Q0698151", "08CZ1M00000"} <= set(parts))
    check("HONDA sheet without TOTAL still read, MRP kept", parts.get("08CZ1M00000", (0, None))[1] == 500)
    check("a sheet with other columns (NOTES) is not mixed in", "CALLBEFOREPICKUP" not in parts and len([p for p in parts if not p.startswith("770")]) == 4)
    print("\n[2] Spares 4U's second file is kept, not replaced")
    check("Renault/Nissan parts still active after the brands file", {"7703083335", "7703077210"} <= set(parts))
    check("6 parts in total", len(parts) == 6)

    print("\n[3] the Renault file again, unchanged -> nothing changes")
    with get_session() as s:
        r = svc.run_import(vid, renault, s)
    imp2, parts2 = active_parts(vid)
    check("unchanged file skipped (no confirmation needed)", r.is_duplicate and imp2.id == imp.id)
    check("all 6 parts still there", parts2 == parts)

    print("\n[4] a new brands file with one part sold out -> only that file's stock changes")
    brands2 = workbook(_TMP / "STOCK_HYUNDAI_TOYOTA_VW_HONDA.xlsx", {
        "HYUNDAI": [head, (1, "95910B4000", "AIR BAG", 2, 9753, 19506)],
        "VW": [head, (1, "5Q0698151", "PAD", 2, 3000, 6000)],
        "HONDA": [("S.NO", "PART NO", "PART NAME", "QTY", "MRP"), (1, "08CZ1M00000", "OIL", 4, 500)],
    })
    with get_session() as s:
        svc.run_import(vid, brands2, s)
    imp3, parts3 = active_parts(vid)
    check("sold-out 71504B4C10 gone", "71504B4C10" not in parts3)
    check("new quantity for 95910B4000", parts3.get("95910B4000", (0,))[0] == 2)
    check("Renault/Nissan untouched", parts3.get("7703077210", (0,))[0] == 3 and "7703083335" in parts3)

    print("\n[4b] the same Renault list under a NEW NAME (phone adds _4_) replaces it, no doubling")
    renamed = workbook(_TMP / "STOCK_RENAULT_AND_NISSAN_4_.xlsx", {
        "Sheet1": [("S.NO", "PART NUMBER", "PART NAME", "QTY", "MRP", "TOTAL"),
                   (1, "7703083335", "CLIP", 5, 89, 445), (2, "7703077210", "CLIP 2", 3, 43, 129)],
    })
    with get_session() as s:
        svc.run_import(vid, renamed, s)
    with get_session() as s:
        imp4 = s.execute(select(InventoryImport).where(InventoryImport.vendor_id == vid, InventoryImport.is_active.is_(True))).scalar_one()
        rows4 = s.execute(select(VendorInventory).where(VendorInventory.import_id == imp4.id)).scalars().all()
        nums = [r.normalized_part_number for r in rows4]
        qty4 = {r.normalized_part_number: int(r.quantity_available) for r in rows4}
    check("no part listed twice", len(nums) == len(set(nums)))
    check("new Renault quantity (5) used", qty4.get("7703083335") == 5)
    check("brand-sheet parts kept", "95910B4000" in nums and "08CZ1M00000" in nums)

    print("\n[5] a normal vendor is unchanged: a new file replaces the old one")
    with get_session() as s:
        svc.run_import(oid, renault, s)
    with get_session() as s:
        svc.run_import(oid, brands, s)
    _imp, op = active_parts(oid)
    check("Other Motors: only the latest file", "7703083335" not in op and "95910B4000" in op)

    print("\n[6] Dealer Portal delta: full list, sold-out part sent as 0")
    from backend.app.integrations.dealer_portal import delta
    from backend.app.integrations.dealer_portal.credentials import DealerPortalAccount

    acct = DealerPortalAccount(
        key="SPARES4U", username="x", password="y", token=None, device_id="d",
        vendor_patterns=["Spares 4U"], full_snapshot=True, tat_days=3, enabled=True,
        vendor_ids=[vid], vendor_names=["Spares 4U"],
    )
    if acct is None:
        print("  (skipped: account shape differs)")
    else:
        with get_session() as s:
            res = delta.build_delta(acct, s)
        sent = {r.part_no: (r.quantity, r.price) for r in res.rows}
        check("changed Renault quantity sent (7703083335 -> 5)", sent.get("7703083335", (None,))[0] == 5)
        check("no part sent twice", len(res.rows) == len({r.part_no for r in res.rows}))
        check("Renault parts sent too (full list)", "7703077210" in sent)
        check("price = MRP (9753) when the file has no price", sent.get("95910B4000", (0, 0))[1] == 9753)

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
