"""Google Sheets Sync: pushes a vendor's current active inventory
(`core.services.inventory_import_service.get_active_inventory`, read-only,
no new business logic) to a worksheet named after that vendor in a shared
Google Sheet. Called from
`backend/app/services/document_processor/dispatcher.py` right after a
successful (non-duplicate) inventory import -- since both manual upload and
WhatsApp inventory import already funnel through that one function, this
covers both automatically with a single call site.

`sync_vendor_inventory_to_sheet` raises on any failure (not configured,
network error, bad credentials); `sync_vendor_inventory_to_sheet_safe` is
what callers should actually use -- it catches everything and records the
outcome on the Google Sheets integration status row instead, so a Sheets
outage never fails the inventory import itself."""

from __future__ import annotations

import os

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.app.integrations.google_sheets import status_service
from backend.app.integrations.google_sheets.config import SHEETS_SCOPE, google_sheets_settings
from backend.app.notifications import emitters as notifications
from core.db import get_session
from core.time_utils import now_ist
from core.logging_setup import get_logger
from core.services import inventory_import_service, vendor_service

logger = get_logger(__name__)

_HEADERS = ["Vendor Part Number", "Description", "Quantity Available", "Price", "MRP", "Last Updated"]

# Bounded HTTP timeouts for the Sheets API (seconds). Overridable via env for
# a slow network, but never unbounded.
SHEETS_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("GOOGLE_SHEETS_CONNECT_TIMEOUT", "10"))
SHEETS_READ_TIMEOUT_SECONDS = float(os.environ.get("GOOGLE_SHEETS_READ_TIMEOUT", "30"))


class GoogleSheetsNotConfiguredError(Exception):
    """Raised when sync is enabled but `GOOGLE_SHEET_ID` and the shared Gmail
    OAuth credentials (`GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` /
    `GMAIL_REFRESH_TOKEN`) aren't all configured."""


def _build_client():
    # Imported lazily so a deployment that never enables Sheets sync doesn't
    # need `gspread`/`google-auth` importable at all.
    import gspread
    from google.oauth2.credentials import Credentials

    # Reuse the SAME OAuth refresh token the Gmail integration already uses
    # (see `backend/app/integrations/gmail/client.py`); no separate service
    # account. The token must carry the Sheets scope (see config.py).
    credentials = Credentials(
        None,
        refresh_token=google_sheets_settings.refresh_token,
        client_id=google_sheets_settings.client_id,
        client_secret=google_sheets_settings.client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=[SHEETS_SCOPE],
    )
    client = gspread.authorize(credentials)
    # Finite (connect, read) timeout so an unreachable or hanging Sheets API can
    # never stall the caller indefinitely. This sync already runs AFTER the
    # inventory transaction has closed (see document_processor/processor.py), so
    # a slow Sheets call cannot hold a DB transaction open -- this bounds the
    # remaining wall-clock cost as well.
    client.set_timeout((SHEETS_CONNECT_TIMEOUT_SECONDS, SHEETS_READ_TIMEOUT_SECONDS))
    return client


def _get_or_create_worksheet(spreadsheet, title: str, num_cols: int):
    import gspread

    try:
        return spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=200, cols=num_cols)


def _row_description(raw_data) -> str:
    if isinstance(raw_data, dict):
        for key, value in raw_data.items():
            if key.strip().lower() in {"description", "part description", "item description"}:
                return str(value or "")
    return ""


def test_connection() -> str:
    """Opens the configured spreadsheet without writing anything -- used
    only by the interactive "Test Connection" action on the Integration
    Status page. Returns the spreadsheet's title on success; raises
    otherwise."""
    if not google_sheets_settings.is_configured():
        raise GoogleSheetsNotConfiguredError(
            "GOOGLE_SHEET_ID or the shared Gmail OAuth credentials "
            "(GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET/GMAIL_REFRESH_TOKEN) are not configured."
        )
    client = _build_client()
    spreadsheet = client.open_by_key(google_sheets_settings.sheet_id)
    return spreadsheet.title


def sync_vendor_inventory_to_sheet(vendor_id: int, session: Session) -> None:
    if not google_sheets_settings.is_configured():
        raise GoogleSheetsNotConfiguredError(
            "Google Sheets sync is enabled but GOOGLE_SHEET_ID or the shared Gmail OAuth "
            "credentials (GMAIL_CLIENT_ID/GMAIL_CLIENT_SECRET/GMAIL_REFRESH_TOKEN) are not "
            "configured (see .env.example)."
        )

    vendor = vendor_service.get_vendor(vendor_id, session)
    if vendor is None:
        raise ValueError(f"Vendor {vendor_id} not found.")

    rows = inventory_import_service.get_active_inventory(vendor_id, session)

    client = _build_client()
    spreadsheet = client.open_by_key(google_sheets_settings.sheet_id)

    # EXACT COPY of the vendor's own file: original columns, original order,
    # original values -- nothing renamed or filtered. Identical rule to the
    # consolidated Vendor_Inventory.xlsx (one format for both outputs); the
    # normalized columns remain internal, for comparison/allocation only.
    raw_headers, raw_rows = inventory_import_service.get_active_raw_table(vendor_id, session)
    if raw_headers:
        values = [raw_headers] + raw_rows
    else:
        # Legacy rows without captured raw cells: normalized fallback.
        synced_at = now_ist().strftime("%Y-%m-%d %H:%M IST")
        values = [_HEADERS] + [
            [
                row.vendor_part_number,
                _row_description(row.raw_data),
                str(row.quantity_available),
                str(row.price) if row.price is not None else "",
                str(row.mrp) if row.mrp is not None else "",
                synced_at,
            ]
            for row in rows
        ]

    # Canonical worksheet identity is the permanent Vendor Code (MA_CT, DE_CT),
    # exactly like the Excel workbook (`core/services/vendor_inventory_workbook.
    # py:_sheet_title`) -- one identity rule for both outputs, never the
    # display name. Created wide enough for however many columns the vendor's
    # file actually has.
    worksheet_title = (vendor.vendor_code or vendor.name or f"V{vendor.id}").strip()
    worksheet = _get_or_create_worksheet(
        spreadsheet, worksheet_title, max(len(values[0]), len(_HEADERS))
    )

    # Overwrite the data range and re-apply header formatting -- simplest
    # correct way to "update existing rows" when the vendor's inventory can
    # change size (rows added/removed) between imports, while leaving
    # everything else about the sheet (other worksheets, column widths set
    # by hand) untouched.
    worksheet.clear()
    worksheet.update(values, "A1")
    last_column = chr(ord("A") + max(len(values[0]), 1) - 1)
    worksheet.format(f"A1:{last_column}1", {"textFormat": {"bold": True}})


def reset_sheet_for_new_day() -> None:
    """Daily reset (Founder rule: ONLY same-day uploads count): delete every
    worksheet whose title is a registered Vendor Code but whose vendor has
    not submitted stock since IST midnight. A vendor's tab reappears the
    moment their file imports today. Worksheet titles that are not a Vendor
    Code (the Founder's hand-made hub tabs) are never touched."""
    if not (google_sheets_settings.enabled and google_sheets_settings.is_configured()):
        logger.info("Sheet daily reset: sync not enabled/configured -- skipping.")
        return

    # WhatsApp's daily-cycle helper defines "submitted today"; imported
    # lazily to keep integration packages decoupled at import time.
    from backend.app.integrations.whatsapp.daily_stock import vendors_submitted_today
    from core.models import Vendor
    from sqlalchemy import select

    with get_session() as session:
        submitted = vendors_submitted_today(session)
        stale_codes = {
            code.strip()
            for vendor_id_, code in session.execute(
                select(Vendor.id, Vendor.vendor_code).where(Vendor.vendor_code.is_not(None))
            )
            if vendor_id_ not in submitted and code and code.strip()
        }

    client = _build_client()
    spreadsheet = client.open_by_key(google_sheets_settings.sheet_id)
    removed = []
    for worksheet in spreadsheet.worksheets():
        title = worksheet.title.strip()
        if title not in stale_codes:
            continue
        try:
            spreadsheet.del_worksheet(worksheet)
            removed.append(title)
        except Exception:  # noqa: BLE001 -- one stubborn tab must not stop the rest
            logger.exception("Sheet daily reset: could not delete worksheet %r.", title)

    logger.info("Sheet daily reset: removed %d stale vendor tab(s): %s", len(removed), removed)
    if removed:
        notifications.broker.publish(
            "info",
            "Google Sheet daily reset completed.",
            f"Removed {len(removed)} vendor tab(s) with no upload today: {', '.join(removed)}",
        )


def reset_sheet_for_new_day_safe() -> None:
    """Scheduler entry point -- never raises."""
    try:
        reset_sheet_for_new_day()
    except Exception:  # noqa: BLE001 -- a reset failure must never hurt anything else
        logger.exception("Google Sheet daily reset failed.")


def sync_vendor_inventory_to_sheet_safe(vendor_id: int, session: Session) -> None:
    if not google_sheets_settings.enabled:
        return

    vendor = vendor_service.get_vendor(vendor_id, session)
    vendor_name = vendor.name if vendor else None

    try:
        sync_vendor_inventory_to_sheet(vendor_id, session)
    except Exception as exc:  # noqa: BLE001 -- a Sheets failure must never fail the import itself
        logger.exception("Google Sheets sync failed for vendor %s", vendor_id)
        with get_session() as status_session:
            status_service.record_sync(
                status_session, success=False, message=str(exc), vendor_name=vendor_name
            )
        notifications.publish_sheet_sync(False, vendor_name, str(exc))
        return

    with get_session() as status_session:
        status_service.record_sync(
            status_session,
            success=True,
            message=f"Synced {vendor_name!r}'s active inventory.",
            vendor_name=vendor_name,
        )
    notifications.publish_sheet_sync(True, vendor_name)
