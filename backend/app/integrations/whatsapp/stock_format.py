"""Ask for the stock file in OUR format when a vendor's file is missing a
required column (24 Oct 2026, Mohan Spares).

Our format, one row per part:

    Part No | Part Description | Brand | Stock Qty | MRP

Without MRP (or Rate) a part cannot go to the Dealer Portal -- it would show
at Rs 0 -- so Mohan's files without MRP left 84-240 parts off the Portal
every day. After a successful import the bot checks the vendor's rows; when
a required column is missing it, once per vendor per day:

  1. tells the vendor (every registered number) which column is missing and
     the format to send -- with a sample Excel when the chat is open;
  2. tells the purchase team (STOCK_FORMAT_TEAM_NUMBERS: Seema, Alam,
     Rajkumar) to get the vendor to send it that way.

Own warehouses (Bijwasan, Jaipur ...) are never asked. Best-effort: never
raises into the import. STOCK_FORMAT_CHECK_ENABLED=false turns it off.
"""

from __future__ import annotations

import io
import os
import threading

from core.logging_setup import get_logger
from core.time_utils import now_ist

logger = get_logger(__name__)

FORMAT_HEADERS = ["Part No", "Part Description", "Brand", "Stock Qty", "MRP"]
FORMAT_LINE = " | ".join(FORMAT_HEADERS)
_DEFAULT_TEAM = "Seema:9289015770,Alam:9217030408,Rajkumar:9217396458"

_asked: set[tuple[int, str]] = set()  # (vendor_id, IST date) already asked today
_lock = threading.Lock()


def _enabled() -> bool:
    return (os.environ.get("STOCK_FORMAT_CHECK_ENABLED") or "true").strip().lower() == "true"


def team_numbers() -> list[str]:
    """STOCK_FORMAT_TEAM_NUMBERS: "Name:number" or plain numbers, comma-separated."""
    from backend.app.integrations.whatsapp.registry import normalize_number

    raw = os.environ.get("STOCK_FORMAT_TEAM_NUMBERS") or _DEFAULT_TEAM
    numbers = []
    for item in raw.split(","):
        number = normalize_number(item.rsplit(":", 1)[-1])
        if number and number not in numbers:
            numbers.append(number)
    return numbers


def missing_columns(rows) -> list[str]:
    """Which of OUR required columns the vendor's active rows lack. Part No
    and Stock Qty are needed for the import itself, so only MRP and Part
    Description can be missing here."""
    from backend.app.integrations.google_sheets.dealer_stock import _description

    if not rows:
        return []
    missing = []
    if not any(r.mrp is not None or r.price is not None for r in rows):
        missing.append("MRP")
    if not any(_description(r.raw_data) for r in rows):
        missing.append("Part Description")
    return missing


def vendor_message(vendor_name: str, missing: list[str]) -> str:
    return (
        f"Namaste {vendor_name} ji,\n"
        f"Aaj ki stock file mili, dhanyavaad. Isme {' aur '.join(missing)} column nahi hai, "
        "isliye kuch parts CarTrends portal par nahi ja paaye.\n"
        "Kripya stock Excel mein is format mein bhejiye:\n"
        f"{FORMAT_LINE}\n"
        "Har part ki ek line, MRP zaroor likhiye."
    )


def team_message(vendor_name: str, missing: list[str], file_name: str, vendor_told: bool) -> str:
    told = (
        "Vendor ko bot ne bata diya hai."
        if vendor_told
        else "Is vendor ka number bot mein registered nahi hai, isliye bot vendor ko nahi bata paaya."
    )
    return (
        f"📋 {vendor_name} ki stock file humare format mein nahi hai.\n"
        f"File: {file_name}\n"
        f"Missing: {', '.join(missing)}\n"
        f"{told} Aap bhi vendor ko boliye ki stock Excel is format mein bheje:\n"
        f"{FORMAT_LINE}\n"
        "Bina MRP ke parts Dealer Portal par nahi jaate."
    )


def sample_excel() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Stock"
    ws.append(FORMAT_HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.append(["0910310216", "BOLT", "MARUTI", 28, 89])
    for col, width in zip("ABCDE", (18, 32, 12, 10, 10)):
        ws.column_dimensions[col].width = width
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def check_and_ask(vendor_id: int | None, vendor_name: str | None) -> None:
    """Scheduler-safe entry point, called after a successful stock import.
    Runs on its own thread; never raises."""
    if vendor_id is None or not _enabled():
        return
    threading.Thread(target=_check_and_ask_safe, args=(vendor_id,), daemon=True).start()


def _check_and_ask_safe(vendor_id: int) -> None:
    try:
        _check_and_ask(vendor_id)
    except Exception:  # noqa: BLE001 -- a format reminder must never affect an import
        logger.exception("Stock format check failed for vendor %s.", vendor_id)


def _check_and_ask(vendor_id: int) -> bool:
    """Returns True when the vendor and team were asked."""
    from sqlalchemy import select

    from backend.app.integrations.google_sheets.dealer_stock import is_company_stock
    from backend.app.integrations.whatsapp import outbound
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
    from core.db import get_session
    from core.models import InventoryImport, Vendor
    from core.services.inventory_import_service import get_active_inventory

    today = now_ist().date().isoformat()
    with get_session() as session:
        vendor = session.get(Vendor, vendor_id)
        if vendor is None or is_company_stock(vendor.name, flag=bool(vendor.is_own_stock)):
            return False
        missing = missing_columns(get_active_inventory(vendor_id, session))
        if not missing:
            return False
        name = vendor.name
        file_name = session.execute(
            select(InventoryImport.file_name).where(
                InventoryImport.vendor_id == vendor_id, InventoryImport.is_active.is_(True)
            )
        ).scalar() or "-"
        vendor_numbers = list(
            session.execute(
                select(WhatsAppRegisteredNumber.whatsapp_number).where(
                    WhatsAppRegisteredNumber.vendor_id == vendor_id
                )
            ).scalars()
        )
    with _lock:
        if (vendor_id, today) in _asked:
            return False
        _asked.add((vendor_id, today))

    vendor_told = False
    for number in vendor_numbers:
        vendor_told |= outbound.send_reply_safe(number, vendor_message(name, missing))
        if outbound.in_service_window(number):
            outbound.send_document_safe(
                number,
                sample_excel(),
                "CarTrends_stock_format.xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                caption="Is format mein stock bhejiye",
            )
    for number in team_numbers():
        outbound.send_reply_safe(number, team_message(name, missing, file_name, vendor_told))
    logger.info(
        "Stock format: %s's file %s lacks %s -- asked vendor (%d number(s), told=%s) and the team.",
        name, file_name, missing, len(vendor_numbers), vendor_told,
    )
    return True
