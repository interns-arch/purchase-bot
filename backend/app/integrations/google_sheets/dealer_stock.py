"""The master Google Sheet's stock tabs, in the purchase team's own format.

TWO OUTPUTS, BOTH BUILT FROM THE DATABASE (never read back)
-----------------------------------------------------------
1. One tab per vendor, titled by Vendor Code (MA_CT, BS_CT...). Written in the
   format the team used to make by hand from each vendor's raw file:

       PartNo | Part Description | Stock          (+ MRP, Rate when he gives them)

   `GOOGLE_SHEETS_VENDOR_TAB_FORMAT=exact` restores the old exact copy of the
   vendor's own file instead.

2. `DEALER STOCK` -- every vendor's stock in one list, the "dealer stock" the
   Founder wants on the Dealer Portal:

       Vendor Code | Vendor | PartNo | Part Description | Stock | MRP | Rate | Updated

   Own warehouses (OWN_STOCK_VENDOR_NAME: Bijwasan, Mansarovar...) are left
   out: that stock is CarTrends' own, already on the Portal, not dealer stock.
   Only vendors who sent stock within the freshness window are listed --
   by default TODAY, the same rule the 09:15 reset applies to vendor tabs, so
   the two never disagree. `GOOGLE_SHEETS_DEALER_STOCK_FRESH_DAYS=2` keeps
   the last two days as well.

Part numbers are written as TEXT, so 0050048 keeps its zeros (Excel had
turned it into 50048 in the team's hand-made file of 30 Sep 2026).

The DEALER STOCK rebuild is DEBOUNCED: a burst of vendor files rebuilds it
once, ~20 s after the last one, not once per file. It never raises into an
import.
"""

from __future__ import annotations

import os
import threading
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.logging_setup import get_logger

logger = get_logger(__name__)

VENDOR_TAB_FORMAT = os.environ.get("GOOGLE_SHEETS_VENDOR_TAB_FORMAT", "team").strip().lower()
DEALER_STOCK_ENABLED = os.environ.get("GOOGLE_SHEETS_DEALER_STOCK_ENABLED", "true").strip().lower() == "true"
DEALER_STOCK_TAB = os.environ.get("GOOGLE_SHEETS_DEALER_STOCK_TAB", "DEALER STOCK").strip() or "DEALER STOCK"
FRESH_DAYS = max(0, int(os.environ.get("GOOGLE_SHEETS_DEALER_STOCK_FRESH_DAYS", "0") or 0))
DEBOUNCE_SECONDS = float(os.environ.get("GOOGLE_SHEETS_DEALER_STOCK_DEBOUNCE_SECONDS", "20"))
WRITE_CHUNK_ROWS = 5000

TEAM_HEADERS = ["PartNo", "Part Description", "Stock"]
DEALER_HEADERS = ["Vendor Code", "Vendor", "PartNo", "Part Description", "Stock", "MRP", "Rate", "Updated"]


def _number(value: Decimal | None):
    """A quantity or price as a real number in the Sheet (not text)."""
    if value is None:
        return ""
    return int(value) if value == value.to_integral_value() else float(value)


def _description(raw_data) -> str:
    from core.ingestion.column_detector import DESCRIPTION_HEADERS, normalise_header

    wanted = {normalise_header(h) for h in DESCRIPTION_HEADERS} | {"name", "partname", "itemname"}
    if isinstance(raw_data, dict):
        for key, value in raw_data.items():
            if normalise_header(str(key)) in wanted and str(value or "").strip():
                return " ".join(str(value).split())
    return ""


def team_format_table(vendor_id: int, session: Session) -> tuple[list[str], list[list]]:
    """A vendor's active stock as PartNo / Part Description / Stock, with MRP
    and Rate only when any of his rows carries them."""
    from core.services.inventory_import_service import get_active_inventory

    rows = get_active_inventory(vendor_id, session)
    has_mrp = any(r.mrp is not None for r in rows)
    has_rate = any(r.price is not None for r in rows)
    headers = TEAM_HEADERS + (["MRP"] if has_mrp else []) + (["Rate"] if has_rate else [])
    table = []
    for r in rows:
        line = [str(r.vendor_part_number), _description(r.raw_data), _number(r.quantity_available)]
        if has_mrp:
            line.append(_number(r.mrp))
        if has_rate:
            line.append(_number(r.price))
        table.append(line)
    return headers, table


def fresh_vendor_ids(session: Session) -> set[int]:
    """Vendors whose stock counts as current -- today by default."""
    from backend.app.integrations.whatsapp.daily_stock import (
        _SUBMITTED_STATUSES,
        _ist_today_start_utc,
        vendors_submitted_today,
    )
    from core.models import InventoryImport

    if FRESH_DAYS == 0:
        return vendors_submitted_today(session)
    start = _ist_today_start_utc() - timedelta(days=FRESH_DAYS)
    return set(
        session.execute(
            select(InventoryImport.vendor_id)
            .where(InventoryImport.created_at >= start, InventoryImport.status.in_(_SUBMITTED_STATUSES))
            .distinct()
        ).scalars()
    )


def dealer_stock_table(session: Session) -> tuple[list[str], list[list]]:
    from core.models import InventoryImport, Vendor, VendorInventory
    from core.services.own_stock import is_own_stock_vendor

    fresh = fresh_vendor_ids(session)
    vendors = [
        v for v in session.execute(select(Vendor).where(Vendor.id.in_(fresh)).order_by(Vendor.vendor_code)).scalars()
        if not is_own_stock_vendor(v.name, flag=bool(v.is_own_stock))
    ]
    table = []
    for vendor in vendors:
        active = session.execute(
            select(InventoryImport).where(InventoryImport.vendor_id == vendor.id, InventoryImport.is_active.is_(True))
        ).scalar_one_or_none()
        if active is None:
            continue
        stamp = active.created_at.strftime("%d-%m-%Y %H:%M") if active.created_at else ""
        for r in session.execute(
            select(VendorInventory).where(VendorInventory.import_id == active.id).order_by(VendorInventory.row_number)
        ).scalars():
            table.append(
                [
                    vendor.vendor_code or "",
                    vendor.name,
                    str(r.vendor_part_number),
                    _description(r.raw_data),
                    _number(r.quantity_available),
                    _number(r.mrp),
                    _number(r.price),
                    stamp,
                ]
            )
    return DEALER_HEADERS, table


def write_tab(spreadsheet, title: str, headers: list[str], table: list[list]) -> None:
    """Replace a tab's contents, sized to fit, part numbers kept as text."""
    import gspread

    rows_needed = len(table) + 1
    cols_needed = len(headers)
    try:
        worksheet = spreadsheet.worksheet(title)
        if worksheet.row_count < rows_needed or worksheet.col_count < cols_needed:
            worksheet.resize(rows=max(worksheet.row_count, rows_needed), cols=max(worksheet.col_count, cols_needed))
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=title, rows=max(rows_needed, 100), cols=cols_needed)
    worksheet.clear()
    # RAW: values are stored exactly as given -- "0050048" stays text, never a
    # number Sheets strips the zeros from.
    worksheet.update([headers], "A1", value_input_option="RAW")
    for start in range(0, len(table), WRITE_CHUNK_ROWS):
        chunk = table[start : start + WRITE_CHUNK_ROWS]
        worksheet.update(chunk, f"A{start + 2}", value_input_option="RAW")
    last = chr(ord("A") + cols_needed - 1)
    worksheet.format(f"A1:{last}1", {"textFormat": {"bold": True}})


def rebuild_dealer_stock() -> int:
    """Rebuild the DEALER STOCK tab now. Returns the number of rows written."""
    from backend.app.integrations.google_sheets.config import google_sheets_settings
    from backend.app.integrations.google_sheets.sync_service import _build_client
    from core.db import get_session

    if not (DEALER_STOCK_ENABLED and google_sheets_settings.enabled and google_sheets_settings.is_configured()):
        return 0
    with get_session() as session:
        headers, table = dealer_stock_table(session)
    spreadsheet = _build_client().open_by_key(google_sheets_settings.sheet_id)
    write_tab(spreadsheet, DEALER_STOCK_TAB, headers, table)
    logger.info("Google Sheet %r rebuilt: %d row(s).", DEALER_STOCK_TAB, len(table))
    return len(table)


def rebuild_dealer_stock_safe() -> None:
    try:
        rebuild_dealer_stock()
    except Exception:  # noqa: BLE001 -- an output must never hurt an import
        logger.exception("Could not rebuild the %r tab.", DEALER_STOCK_TAB)


_timer: threading.Timer | None = None
_lock = threading.Lock()


def request_rebuild() -> None:
    """Ask for a DEALER STOCK rebuild; a burst of requests becomes one."""
    global _timer
    if not DEALER_STOCK_ENABLED:
        return
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(DEBOUNCE_SECONDS, rebuild_dealer_stock_safe)
        _timer.daemon = True
        _timer.start()
