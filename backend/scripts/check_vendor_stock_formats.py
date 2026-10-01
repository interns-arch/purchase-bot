"""Self-contained check of vendor stock intake -- every format a vendor sends.

Runs against a THROWAWAY SQLite file and never messages anyone:

    venv\\Scripts\\python.exe -m backend.scripts.check_vendor_stock_formats

Exit code 0 = every check passed. Each part of "case 1" (stock in any
format -> Dealer Stock sheet -> Dealer Portal) adds its own section here."""

from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vendor-stock-check-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["AI_FALLBACK_ENABLED"] = "false"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_ADMIN_PHONE_NUMBER"] = ""

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def _write_csv(path: Path, rows: list[list[str]]) -> Path:
    import csv

    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return path


def _import(vendor_name: str, path: Path):
    from sqlalchemy import select

    from core.db import get_session
    from core.models import Vendor, VendorInventory
    from core.services import inventory_import_service as imp

    with get_session() as s:
        vendor = Vendor(name=vendor_name, vendor_code=vendor_name[:3].upper() + "_CT")
        s.add(vendor)
        s.flush()
        vendor_id = vendor.id
    with get_session() as s:
        result = imp.run_import(vendor_id, path, s)
        status = getattr(result.status, "value", result.status)
    with get_session() as s:
        rows = s.execute(select(VendorInventory).where(VendorInventory.vendor_id == vendor_id)).scalars().all()
        stock = {r.vendor_part_number: r.quantity_available for r in rows}
    return status, stock


def main() -> int:
    from core.db import init_db
    from core.ingestion.column_detector import (
        is_parseable_stock_quantity,
        parse_quantity,
        parse_stock_quantity,
    )

    import backend.app.main  # noqa: F401 -- registers every table

    init_db(force=True)

    print("\n[1] stock quantities written with their unit")
    check('"4SET" -> 4', parse_stock_quantity("4SET") == Decimal("4"))
    check('"2 PCS" -> 2', parse_stock_quantity("2 PCS") == Decimal("2"))
    check('"10 Nos." -> 10', parse_stock_quantity("10 Nos.") == Decimal("10"))
    check('"1,200 pcs" -> 1200', parse_stock_quantity("1,200 pcs") == Decimal("1200"))
    check('"5 LTR" is NOT a count -- refused, never guessed', not is_parseable_stock_quantity("5 LTR"))
    check('"2 KG" is NOT a count -- refused', not is_parseable_stock_quantity("2 KG"))
    check('"SET" alone has no number -- refused', not is_parseable_stock_quantity("SET"))
    check("the shared parse_quantity is unchanged (orders, prices, MRP)", parse_quantity("4SET") == Decimal("0"))
    path = _write_csv(
        _TMP / "units.csv",
        [["PartNo", "Part Description", "Qty"], ["262728992A", "BULB", "4SET"], ["7703083335", "CLIP", "1"], ["X-5LTR", "OIL", "5 LTR"], ["NEG1", "CLIP", "-3 pcs"]],
    )
    status, stock = _import("Units Vendor", path)
    check("an Excel/CSV row with '4SET' imports as 4", stock.get("262728992A") == Decimal("4"))
    check("a plain number still imports", stock.get("7703083335") == Decimal("1"))
    check("'5 LTR' row is rejected, not imported", "X-5LTR" not in stock)
    check("'-3 pcs' is still refused as negative stock", "NEG1" not in stock)
    check("the import reports the refused rows", status == "COMPLETED_WITH_ERRORS")

    print("\n" + ("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
