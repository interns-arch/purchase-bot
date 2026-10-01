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

    _check_typed_stock()

    print("\n" + ("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED"))
    return 0 if failures == 0 else 1


def _check_typed_stock() -> None:
    """[2] a registered vendor TYPES his stock on WhatsApp."""
    from sqlalchemy import select

    from backend.app.integrations.whatsapp import vendor_text_stock as vts
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
    from backend.app.integrations.whatsapp.parser import IncomingWhatsAppText
    from backend.app.workers import document_worker as worker
    from core.db import get_session
    from core.models import Vendor
    from core.services.inventory_import_service import get_active_inventory, get_active_raw_table

    print("\n[2] stock typed as a WhatsApp message")
    r = vts.parse_stock_text("16510M68K10 5\n2630002752 x 12\nTT-100 oil filter 3 pcs\n1701AAA06701N - 0")
    check("four lines read, with a description kept", [(l.part_number, int(l.quantity)) for l in r.lines] == [("16510M68K10", 5), ("2630002752", 12), ("TT-100", 3), ("1701AAA06701N", 0)] and r.lines[2].description == "oil filter")
    check("0 stays 0 (sold out), never becomes 1", r.lines[3].quantity == 0)
    r = vts.parse_stock_text("16510M68K10\n2630002752 6")
    check("a part with NO quantity is reported, never imported as 1", [l.part_number for l in r.lines] == ["2630002752"] and r.unreadable == ["16510M68K10"])
    check('"good morning sir" is not a stock list', not vts.parse_stock_text("good morning sir").is_stock_list)
    check('"16510M68K10 kal aayega" is not a stock list', not vts.parse_stock_text("16510M68K10 kal aayega").is_stock_list)
    r = vts.parse_stock_text("16510M68K10 5\n16510M68K10 7")
    check("the same part twice is one line at the larger figure, never added", len(r.lines) == 1 and r.lines[0].quantity == 7)

    # A vendor with real stock on file, shaped exactly like the raw Mahindra
    # export the purchase team receives (30 Sep 2026): two title rows, then
    # Part No. / Name / Closing Stock / Rate, and a TOTAL line at the bottom.
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(["MAHINDRA DELHI"])
    sheet.append(["Stock Status Till 30/09/2026"])
    sheet.append(["Part No.", "Name", "Closing Stock", "Rate"])
    sheet.append(["1701AAA06701N", " HEAD LAMP LH WO HLL M", 2, 2835])
    sheet.append(["0703DD2730N", "2ND GEAR ASSY V4 NGT 5", 2, 3990])
    for i in range(48):
        sheet.append([f"0606CB{i:04d}N", f"PART {i}", (i % 9) + 1, 37 + i])
    sheet.append([None, None, 23205, None])  # the export's total line
    source = _TMP / "mahindra_raw.xlsx"
    book.save(source)
    status, before = _import("Mahindra Typed", source)
    check("the raw Mahindra-style file imports all 50 parts (title rows and total skipped)", len(before) == 50)
    with get_session() as s:
        vendor_id = s.execute(select(Vendor.id).where(Vendor.name == "Mahindra Typed")).scalar_one()
        s.add(WhatsAppRegisteredNumber(whatsapp_number="919555000111", vendor_id=vendor_id))
    replies: list[tuple[str, str]] = []
    worker.send_reply_safe = lambda to, body: replies.append((to, body))
    vts.ENABLED = True

    def text(body: str) -> None:
        worker._handle_incoming_whatsapp_text(IncomingWhatsAppText(sender="919555000111", message_id="m1", text=body))

    replies.clear()
    text("good morning sir")
    with get_session() as s:
        same = {r.vendor_part_number: r.quantity_available for r in get_active_inventory(vendor_id, s)}
    check("chatter from a registered vendor changes nothing and gets no reply", same == before and not replies)

    replies.clear()
    text("aaj ka stock\n1701AAA06701N 7\n0703DD2730N 0\nNEWPART1234 head lamp 4")
    with get_session() as s:
        after = {r.vendor_part_number: r.quantity_available for r in get_active_inventory(vendor_id, s)}
        headers, table = get_active_raw_table(vendor_id, s)
    check("a typed quantity updates that part", after.get("1701AAA06701N") == Decimal("7"))
    check("a typed 0 marks that part sold out", after.get("0703DD2730N") == Decimal("0"))
    check("a new part is added", after.get("NEWPART1234") == Decimal("4"))
    untouched = [p for p in before if p not in {"1701AAA06701N", "0703DD2730N"}]
    check(f"his other {len(untouched)} parts are unchanged (not wiped)", all(after.get(p) == before[p] for p in untouched))
    check("his own file layout and Rate column are kept", "Rate" in headers)
    rate_col = headers.index("Rate")
    part_col = next(i for i, h in enumerate(headers) if h.lower().startswith("part"))
    rates = {row[part_col]: row[rate_col] for row in table}
    check("...including the Rate of the part he just updated", rates.get("1701AAA06701N") not in (None, ""))
    reply = replies[-1][1] if replies else ""
    check("the vendor is told exactly what changed", "1701AAA06701N 7" in reply and "NEWPART1234 4" in reply and "unchanged" in reply)


if __name__ == "__main__":
    sys.exit(main())
