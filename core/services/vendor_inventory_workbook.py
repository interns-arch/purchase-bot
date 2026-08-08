"""Consolidated Vendor Inventory workbook generator.

Builds ONE openpyxl workbook with one worksheet per vendor (named by the
vendor's existing Vendor Code, e.g. AR_CT), each containing that vendor's
latest ACTIVE inventory read straight from the database -- the database
stays the source of truth; this is a read-only projection of it.

This is the "output/synchronisation layer" for the temporary WhatsApp-Excel
path, deliberately separate from both the import logic and the Google Sheets
sync so either output can operate independently. The row/column shape mirrors
the Google Sheets sync (`backend/app/integrations/google_sheets/sync_service.py`)
so the temporary Excel and the future Sheets output stay consistent.

Pure business logic -- no FastAPI/WhatsApp/print()/input() here.
"""

from __future__ import annotations

import io
import re
from datetime import datetime, timezone

import openpyxl
from openpyxl.styles import Font
from sqlalchemy.orm import Session

from core.services import inventory_import_service, vendor_service

WORKBOOK_FILENAME = "Vendor_Inventory.xlsx"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Same columns the Google Sheets sync writes, so the two outputs match.
_HEADERS = ["Vendor Part Number", "Description", "Quantity Available", "Price", "MRP", "Last Updated"]

# Excel worksheet titles: max 31 chars and none of : \ / ? * [ ]
_INVALID_SHEET_CHARS = re.compile(r"[:\\/?*\[\]]")
_MAX_SHEET_TITLE = 31


def _sheet_title(vendor, used: set[str]) -> str:
    """Worksheet name from the vendor's permanent Vendor Code (falls back to a
    sanitised name / id only if a legacy vendor has no code). Sanitised to
    Excel's rules and de-duplicated within this workbook."""
    base = (vendor.vendor_code or vendor.name or f"V{vendor.id}").strip()
    base = _INVALID_SHEET_CHARS.sub("_", base)[:_MAX_SHEET_TITLE] or f"V{vendor.id}"
    title = base
    suffix = 2
    while title.lower() in used:
        # Keep within 31 chars while making it unique (extremely unlikely for codes).
        title = f"{base[: _MAX_SHEET_TITLE - 2]}_{suffix}"
        suffix += 1
    used.add(title.lower())
    return title


def _description(raw_data) -> str:
    if isinstance(raw_data, dict):
        for key, value in raw_data.items():
            if key.strip().lower() in {"description", "part description", "item description"}:
                return str(value or "")
    return ""


def build_workbook(session: Session) -> openpyxl.Workbook:
    """One workbook, one worksheet per vendor (by Vendor Code), each holding
    that vendor's latest active inventory from the DB. Includes ALL vendors in
    the database -- not only the one that just imported -- so the workbook
    always represents the current full inventory state."""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)  # drop the default blank sheet

    synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    used_titles: set[str] = set()

    vendors = vendor_service.list_vendors(session)
    for vendor in vendors:
        sheet = workbook.create_sheet(title=_sheet_title(vendor, used_titles))
        sheet.append(_HEADERS)
        for cell in sheet[1]:
            cell.font = Font(bold=True)

        for row in inventory_import_service.get_active_inventory(vendor.id, session):
            sheet.append(
                [
                    row.vendor_part_number,
                    _description(row.raw_data),
                    str(row.quantity_available),
                    str(row.price) if row.price is not None else "",
                    str(row.mrp) if row.mrp is not None else "",
                    synced_at,
                ]
            )

        for column_cells in sheet.columns:
            width = max((len(str(c.value)) for c in column_cells if c.value is not None), default=10)
            sheet.column_dimensions[column_cells[0].column_letter].width = width + 2

    # An empty workbook is invalid; guarantee at least one sheet. In practice
    # this path is only ever reached after a successful import (>=1 vendor).
    if not workbook.sheetnames:
        workbook.create_sheet(title="Vendor_Inventory")

    return workbook


def workbook_to_bytes(workbook: openpyxl.Workbook) -> bytes:
    """Serialise to bytes in memory -- no temp file to store or clean up."""
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
