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
os.environ["OWN_STOCK_VENDOR_NAME"] = "Bijwasan"

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
        import uuid

        vendor = Vendor(name=vendor_name, vendor_code="T" + uuid.uuid4().hex[:6].upper() + "_CT")
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
    _check_pdf_stock()
    _check_docx_txt()
    _check_photos()
    _check_sheet_tabs()

    print("\n" + ("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED"))
    return 0 if failures == 0 else 1


def _pdfs() -> dict[str, Path]:
    """Stock lists and a bill, shaped like real dealer printouts."""
    from backend.scripts._pdf_fixture import make_pdf, table_page

    head = ["Part No.", "Name", "Closing Stock", "Rate"]
    rows = [[f"0606CB{i:04d}N", f"PART {i}", str((i % 9) + 1), str(37 + i)] for i in range(60)]
    cols = [40, 150, 330, 450]
    out = {}
    # Mahindra-style: no ruling lines, two pages, header repeated on page 2.
    out["stock"] = make_pdf(
        _TMP / "mahindra_stock.pdf",
        [
            table_page([head] + rows[:40], cols, title=["MAHINDRA DELHI", "Stock Status Till 30/09/2026"]),
            table_page([head] + rows[40:], cols),
        ],
    )
    # Maruti DMS-style: bordered, with MRP beside the stock.
    out["bordered"] = make_pdf(
        _TMP / "maruti_stock.pdf",
        [
            table_page(
                [["Part Num", "Part Description", "MRP", "Current Stock"]]
                + [[f"01411M{i:04d}A", "BOLT", str(100 + i), str(i % 7)] for i in range(30)],
                cols,
                title=["Part search Details"],
                bordered=True,
            )
        ],
    )
    # A vendor's bill.
    out["invoice"] = make_pdf(
        _TMP / "bill.pdf",
        [
            table_page(
                [["Part No", "Description", "Qty", "Rate", "Amount"], ["16510M68K10", "OIL FILTER", "10", "450", "4500"]],
                [40, 140, 300, 380, 460],
                title=["TAX INVOICE", "Invoice No: SAA/2026/118   Date: 30-09-2026", "GSTIN: 07ABCDE1234F1Z5"],
            )
            | {"text": table_page([["Taxable Value 4500", "CGST 9% 405", "SGST 9% 405", "Grand Total 5310"]], [40, 180, 300, 420], top=600)["text"]
               + table_page([["Part No", "Description", "Qty", "Rate", "Amount"], ["16510M68K10", "OIL FILTER", "10", "450", "4500"]], [40, 140, 300, 380, 460], title=["TAX INVOICE", "Invoice No: SAA/2026/118   Date: 30-09-2026", "GSTIN: 07ABCDE1234F1Z5"])["text"]}
        ],
    )
    # A part/quantity table with no word saying stock or bill.
    out["unclear"] = make_pdf(
        _TMP / "list.pdf",
        [table_page([["Part No", "Qty"]] + [[f"ZX{i:05d}", str(i + 1)] for i in range(5)], [40, 200])],
    )
    # A scanned page: no text at all.
    out["scanned"] = make_pdf(_TMP / "scan.pdf", [{"text": [], "lines": [(40, 400, 500, 400)]}])
    return out


def _check_pdf_stock() -> None:
    """[3] stock lists sent as PDF, told apart from bills."""
    from backend.app.integrations.whatsapp import pdf_choice
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
    from backend.app.integrations.whatsapp.parser import IncomingWhatsAppMessage, IncomingWhatsAppText
    from backend.app.workers import document_worker as worker
    from core.db import get_session
    from core.ingestion.pdf_reader import classify_pdf, read_pdf, read_pdf_rows
    from core.models import Vendor
    from core.services.inventory_import_service import get_active_inventory, get_active_raw_table

    print("\n[3] stock lists sent as PDF")
    pdfs = _pdfs()
    parsed = read_pdf_rows(pdfs["stock"])
    check("a 2-page PDF without ruling lines: all 60 parts read", len(parsed.rows) == 60)
    check("...the header repeated on page 2 is not read as a part", all(r["Part No."] != "Part No." for r in parsed.rows))
    check("...names stay whole ('PART 59', not 'PART' + '59')", parsed.rows[-1]["Name"] == "PART 59")
    check("...and the Rate column is kept", parsed.headers == ["Part No.", "Name", "Closing Stock", "Rate"])
    bordered = read_pdf_rows(pdfs["bordered"])
    check("a bordered PDF table is read", len(bordered.rows) == 30 and bordered.rows[3]["Current Stock"] == "3")

    kinds = {name: classify_pdf(read_pdf(path))[0] for name, path in pdfs.items()}
    check("a stock list is recognised as stock", kinds["stock"] == "stock" and kinds["bordered"] == "stock")
    check("a bill is recognised as a bill", kinds["invoice"] == "invoice")
    check("no word either way -> unclear (the vendor is asked)", kinds["unclear"] == "unclear")
    check("a scanned page is recognised as scanned", kinds["scanned"] == "scanned")

    status, stock = _import("Pdf Direct", pdfs["bordered"])
    check("the importer takes a PDF like an Excel file", status == "COMPLETED" and len(stock) == 30)
    check("...MRP is never read as the quantity", stock.get("01411M0003A") == Decimal("3"))

    # A registered vendor sending PDFs over WhatsApp.
    with get_session() as s:
        vendor = Vendor(name="Pdf Vendor", vendor_code="PWV_CT")
        s.add(vendor)
        s.flush()
        vendor_id = vendor.id
        s.add(WhatsAppRegisteredNumber(whatsapp_number="919555000222", vendor_id=vendor_id))
    replies: list[str] = []
    worker.send_reply_safe = lambda to, body: replies.append(body)
    import shutil

    def send(name: str) -> None:
        copy = _TMP / f"incoming_{name}_{len(replies)}.pdf"
        shutil.copy(pdfs[name], copy)
        worker.download_document_media = lambda media_id, filename, client: copy
        worker.handle_incoming_whatsapp_message(
            IncomingWhatsAppMessage(sender="919555000222", message_id=f"w-{name}", timestamp=None, caption=None, media_id="m", filename=f"{name}.pdf", mime_type="application/pdf")
        )

    def text(body: str) -> None:
        worker._handle_incoming_whatsapp_text(IncomingWhatsAppText(sender="919555000222", message_id="t", text=body))

    def held() -> bool:
        with get_session() as s:
            return pdf_choice.has_pending("919555000222", s)

    replies.clear()
    send("stock")
    with get_session() as s:
        live = get_active_inventory(vendor_id, s)
        headers, _ = get_active_raw_table(vendor_id, s)
    check("a stock PDF from a registered vendor imports as his stock", len(live) == 60 and "Rate" in headers)
    check("...and he is told it was received as stock", bool(replies) and "Stock received" in replies[-1])

    replies.clear()
    send("invoice")
    with get_session() as s:
        still = len(get_active_inventory(vendor_id, s))
    check("a bill does NOT touch his stock", still == 60)
    check("...it goes to invoice checking, as before", bool(replies) and "invoice" in replies[-1].lower())

    replies.clear()
    send("unclear")
    check("an unclear PDF is held, and he is asked 'stock list hai ya bill?'", held() and bool(replies) and "bill" in replies[-1].lower())
    replies.clear()
    text("stock")
    with get_session() as s:
        after = len(get_active_inventory(vendor_id, s))
    check("his reply 'stock' imports the held PDF as stock", not held() and after == 5 and bool(replies) and "Stock received" in replies[-1])

    replies.clear()
    text("stock")
    check("'stock' typed with nothing held changes nothing and gets no reply", not replies)

    replies.clear()
    send("scanned")
    with get_session() as s:
        unchanged = len(get_active_inventory(vendor_id, s))
    check("a scanned PDF is refused and his stock is untouched", unchanged == 5 and bool(replies) and replies[-1].startswith("❌"))


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



def _make_docx(path: Path, tables: list[list[list[str]]], paragraphs: list[str] = ()) -> Path:
    """A minimal real .docx (zip of WordprocessingML), built with the stdlib."""
    import zipfile
    from xml.sax.saxutils import escape

    def cell(text: str) -> str:
        return f"<w:tc><w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p></w:tc>"

    body = "".join(f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    for table in tables:
        body += "<w:tbl>" + "".join("<w:tr>" + "".join(cell(c) for c in row) + "</w:tr>" for row in table) + "</w:tbl>"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("word/document.xml", document)
    return path


def _check_docx_txt() -> None:
    """[4] stock lists sent as a Word document or a text file."""
    print("\n[4] stock lists as Word (.docx) and text (.txt)")
    docx = _make_docx(
        _TMP / "stock.docx",
        [
            [["Note", "for office use"]],  # a table that is NOT the stock table
            [["Part No", "Description", "MRP", "Stock"]] + [[f"WD{i:05d}", "CLIP", str(50 + i), str(i % 4)] for i in range(12)],
        ],
        paragraphs=["ESS AAY AUTOMOTIVE", "Stock as on 30-09-2026"],
    )
    status, stock = _import("Word Vendor", docx)
    check("a Word table is read, skipping a table that is not the stock list", status == "COMPLETED" and len(stock) == 12)
    check("...MRP is not the quantity", stock.get("WD00005") == Decimal("1"))

    table_txt = _TMP / "stock_table.txt"
    table_txt.write_text("Part No\tName\tClosing Stock\n" + "".join(f"TX{i:04d}\tBULB\t{i + 2}\n" for i in range(8)), encoding="utf-8")
    status, stock = _import("Txt Table Vendor", table_txt)
    check("a tab-separated .txt table is read", status == "COMPLETED" and len(stock) == 8 and stock.get("TX0003") == Decimal("5"))

    typed_txt = _TMP / "typed.txt"
    typed_txt.write_text("Aaj ka stock\n16510M68K10 5\nTT-100 oil filter 3 pcs\n1701AAA06701N 0\n2630002752\n", encoding="utf-8")
    status, stock = _import("Txt Typed Vendor", typed_txt)
    check("a .txt typed list is read like a WhatsApp message", stock.get("16510M68K10") == Decimal("5") and stock.get("TT-100") == Decimal("3") and stock.get("1701AAA06701N") == Decimal("0"))
    check("...a part without a quantity is rejected and reported, not imported as 1", "2630002752" not in stock and status == "COMPLETED_WITH_ERRORS")

    junk = _TMP / "notes.txt"
    junk.write_text("Please call me tomorrow regarding payment.\nThanks", encoding="utf-8")
    status, stock = _import("Txt Junk Vendor", junk)
    check("a .txt with no stock in it fails loudly and imports nothing", status == "FAILED" and not stock)



def _check_photos() -> None:
    """[5] a stock list sent as a PHOTO, confirmed with the vendor."""
    import shutil

    from backend.app.ai import vision
    from backend.app.integrations.whatsapp import photo_stock
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
    from backend.app.integrations.whatsapp.parser import IncomingWhatsAppMessage, IncomingWhatsAppText, parse_webhook_payload
    from backend.app.workers import document_worker as worker
    from core.db import get_session
    from core.models import Vendor
    from core.services.inventory_import_service import get_active_inventory

    print("\n[5] stock lists sent as a photo")
    payload = {"entry": [{"changes": [{"value": {"messages": [{"from": "919", "id": "i1", "type": "image", "image": {"id": "MEDIA", "mime_type": "image/jpeg"}}]}}]}]}
    msgs = parse_webhook_payload(payload)
    check("a WhatsApp photo reaches the worker, marked as a photo", len(msgs) == 1 and msgs[0].is_photo and msgs[0].filename.endswith(".jpg"))

    parsed = photo_stock.read_lines("16510M68K10 5\n2630002752 ?\n? 7\n**TT-100 3**")
    check("the model's '?' lines are reported, never imported", [l.part_number for l in parsed.lines] == ["16510M68K10", "TT-100"] and len(parsed.unreadable) == 2)
    check("'haan' / 'nahi' are read as yes / no", photo_stock.answer("haan") == "yes" and photo_stock.answer("Nahi") == "no" and photo_stock.answer("haan 5 din") is None)

    with get_session() as s:
        vendor = Vendor(name="Photo Vendor", vendor_code="PHV_CT")
        s.add(vendor)
        s.flush()
        vendor_id = vendor.id
        s.add(WhatsAppRegisteredNumber(whatsapp_number="919555000333", vendor_id=vendor_id))
    seed = _write_csv(_TMP / "photo_seed.csv", [["PartNo", "Part Description", "Qty"], ["16510M68K10", "OIL FILTER", "1"], ["KEEP0001", "CLIP", "9"]])
    from core.services import inventory_import_service as imp

    with get_session() as s:
        imp.run_import(vendor_id, seed, s)

    replies: list[str] = []
    worker.send_reply_safe = lambda to, body: replies.append(body)
    model_says = {"text": "16510M68K10 5\n2630002752 12\nTT-100 3"}
    vision.read_stock_image = lambda image: (model_says["text"], "fake-vision")
    photo = _TMP / "photo.jpg"
    photo.write_bytes(b"not really a jpeg")
    worker.download_document_media = lambda media_id, filename, client: photo

    def send_photo(sender="919555000333") -> None:
        worker.handle_incoming_whatsapp_message(
            IncomingWhatsAppMessage(sender=sender, message_id="p", timestamp=None, caption=None, media_id="m", filename="photo_m.jpg", mime_type="image/jpeg", is_photo=True)
        )

    def text(body: str, sender="919555000333") -> None:
        worker._handle_incoming_whatsapp_text(IncomingWhatsAppText(sender=sender, message_id="t", text=body))

    def stock() -> dict:
        with get_session() as s:
            return {r.vendor_part_number: r.quantity_available for r in get_active_inventory(vendor_id, s)}

    replies.clear()
    send_photo()
    shown = "\n".join(replies)
    check("he is told the photo arrived, then shown every line read", replies and "Photo mil gayi" in replies[0] and "16510M68K10 — 5" in shown and "TT-100 — 3" in shown and "haan" in replies[-1])
    check("NOTHING is imported before he says haan", stock().get("16510M68K10") == Decimal("1"))

    replies.clear()
    text("haan")
    after = stock()
    check("'haan' imports the photo's lines", after.get("16510M68K10") == Decimal("5") and after.get("TT-100") == Decimal("3"))
    check("...and only those: his other parts are kept", after.get("KEEP0001") == Decimal("9"))
    check("...and he is told what changed", replies and "Stock updated" in replies[-1])

    replies.clear()
    model_says["text"] = "16510M68K10 99"
    send_photo()
    text("nahi")
    check("'nahi' cancels it: nothing changes", stock().get("16510M68K10") == Decimal("5") and replies and "cancel" in replies[-1].lower())

    replies.clear()
    text("haan")
    check("'haan' with no photo waiting is not taken as a confirmation", stock().get("16510M68K10") == Decimal("5"))

    replies.clear()
    model_says["text"] = "I cannot read this image."
    send_photo()
    check("an unreadable photo is reported, nothing held", replies and replies[-1].startswith("❌") and stock().get("16510M68K10") == Decimal("5"))

    replies.clear()
    model_says["text"] = "16510M68K10 5"
    send_photo(sender="919000999888")
    check("a photo from an unregistered number is ignored, as before", not replies)

    # A scanned PDF goes to the same reader, page by page.
    pdfs = _pdfs()
    scanned = _TMP / "scan_in.pdf"
    shutil.copy(pdfs["scanned"], scanned)
    worker.download_document_media = lambda media_id, filename, client: scanned
    model_says["text"] = "SCAN0001 4"
    replies.clear()
    worker.handle_incoming_whatsapp_message(
        IncomingWhatsAppMessage(sender="919555000333", message_id="s", timestamp=None, caption=None, media_id="m", filename="scan.pdf", mime_type="application/pdf")
    )
    check("a scanned PDF is read like a photo and confirmed first", any("SCAN0001 — 4" in r for r in replies) and "SCAN0001" not in stock())
    text("haan")
    check("...and imported on haan", stock().get("SCAN0001") == Decimal("4"))



class _FakeSheet:
    """Records what would be written to Google Sheets."""

    def __init__(self):
        self.tabs: dict[str, dict] = {}

    def worksheet(self, title):
        import gspread

        if title not in self.tabs:
            raise gspread.WorksheetNotFound(title)
        return _FakeTab(self, title)

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = {"rows": rows, "cols": cols, "cells": {}, "raw": []}
        return _FakeTab(self, title)


class _FakeTab:
    def __init__(self, book, title):
        self.book, self.title = book, title
        self.data = book.tabs[title]

    row_count = property(lambda self: self.data["rows"])
    col_count = property(lambda self: self.data["cols"])

    def resize(self, rows, cols):
        self.data.update(rows=rows, cols=cols)

    def clear(self):
        self.data["cells"] = {}

    def update(self, values, start, value_input_option=None):
        self.data["raw"].append(value_input_option)
        first = int(start[1:])
        for offset, row in enumerate(values):
            self.data["cells"][first + offset] = row

    def format(self, *_args, **_kwargs):
        pass

    def rows(self):
        return [self.data["cells"][k] for k in sorted(self.data["cells"])]


def _check_sheet_tabs() -> None:
    """[6] vendor tabs in the team's format, and the DEALER STOCK tab."""
    import time as _time
    from datetime import datetime, timedelta

    from backend.app.integrations.google_sheets import dealer_stock
    from core.db import get_session
    from core.models import InventoryImport, Vendor

    print("\n[6] Google Sheet: team-format vendor tabs and DEALER STOCK")
    from openpyxl import Workbook

    def vendor_with(name, rows, *, yesterday=False):
        book = Workbook()
        sheet = book.active
        sheet.append(["Part No.", "Name", "Closing Stock", "Rate"])
        for r in rows:
            sheet.append(r)
        path = _TMP / f"{name.replace(' ', '_')}.xlsx"
        book.save(path)
        _import(name, path)
        with get_session() as s:
            v = s.execute(select(Vendor).where(Vendor.name == name)).scalar_one()
            if yesterday:
                for imp in s.execute(select(InventoryImport).where(InventoryImport.vendor_id == v.id)).scalars():
                    imp.created_at = datetime.utcnow() - timedelta(days=2)
            return v.id, v.vendor_code

    from sqlalchemy import select

    a_id, a_code = vendor_with("Sheet Vendor A", [["0050048", "BOLT", 3, 12], ["1701AAA06701N", "HEAD LAMP LH", 2, 2835]])
    b_id, _ = vendor_with("Sheet Vendor B", [["TT-100", "OIL FILTER", 7, 450]])
    own_id, _ = vendor_with("Bijwasan Warehouse", [["OWN0001", "CLIP", 40, 5]])
    old_id, _ = vendor_with("Sheet Vendor Old", [["OLD0001", "CLIP", 9, 5]], yesterday=True)

    with get_session() as s:
        headers, table = dealer_stock.team_format_table(a_id, s)
    check("a vendor tab is in the team's format, Rate kept", headers == ["PartNo", "Part Description", "Stock", "Rate"])
    check("...with the description and stock filled in", table[0][:3] == ["0050048", "BOLT", 3])

    with get_session() as s:
        headers, table = dealer_stock.dealer_stock_table(s)
    parts = {row[2] for row in table}
    check("DEALER STOCK lists every vendor who sent stock today", {"0050048", "1701AAA06701N", "TT-100"} <= parts)
    check("...but not the company's own warehouse", "OWN0001" not in parts)
    check("...and not a vendor whose stock is not from today", "OLD0001" not in parts)
    a_rows = [row for row in table if row[0] == a_code]
    check("...with the vendor's code and name on each of his rows", len(a_rows) == 2 and all(row[1] == "Sheet Vendor A" for row in a_rows))

    book = _FakeSheet()
    dealer_stock.write_tab(book, "DEALER STOCK", headers, table)
    written = book.worksheet("DEALER STOCK").rows()
    check("the tab is written header first", written[0] == dealer_stock.DEALER_HEADERS)
    check("part numbers keep their leading zeros (written as text, RAW)", any(r[2] == "0050048" for r in written[1:]) and set(book.tabs["DEALER STOCK"]["raw"]) == {"RAW"})

    calls = []
    real = dealer_stock.rebuild_dealer_stock_safe
    dealer_stock.rebuild_dealer_stock_safe = lambda: calls.append(1)
    dealer_stock.DEBOUNCE_SECONDS = 0.3
    dealer_stock.DEALER_STOCK_ENABLED = True
    for _ in range(5):
        dealer_stock.request_rebuild()
    _time.sleep(0.8)
    dealer_stock.rebuild_dealer_stock_safe = real
    check("a burst of 5 vendor files rebuilds DEALER STOCK once", len(calls) == 1)


if __name__ == "__main__":
    sys.exit(main())
