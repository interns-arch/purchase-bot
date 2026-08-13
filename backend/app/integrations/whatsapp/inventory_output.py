"""Temporary Vendor Inventory output: after a WhatsApp Vendor Inventory file
is successfully imported into the database, build ONE consolidated workbook
(all vendors, one worksheet per Vendor Code) and send it to the Founder's
WhatsApp number. A stand-in for the Google Sheets sync until the OAuth token
carries the spreadsheets scope.

Strictly best-effort and failure-isolated: the DB import is already committed
before this runs, so nothing here can roll it back. Every failure is caught,
logged (never the access token), and surfaced as a toast; the import result is
untouched. Reads the DB in its own session, so the committed import is visible.
"""

from __future__ import annotations

import threading

from backend.app.integrations.whatsapp import outbound
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.notifications import broker
from core.db import get_session
from core.logging_setup import get_logger
from core.services import vendor_inventory_workbook as workbook_service

logger = get_logger(__name__)

# Debounce state for batched sends: when a WhatsApp batch delivers many vendor
# files at once, every successful import calls `request_consolidated_send()`,
# but only the LAST request within the quiet period actually sends -- one
# final workbook per batch instead of one per file. The workbook itself is
# always rebuilt from the database at fire time, so the single send reflects
# every import that landed during the batch.
_debounce_lock = threading.Lock()
_debounce_timer: threading.Timer | None = None
# Vendors whose imports triggered the pending send -- named (no codes) in the
# workbook caption so the Founder sees WHO updated without opening the file.
_pending_vendor_names: set[str] = set()


def request_consolidated_send(vendor_name: str | None = None) -> None:
    """Debounced entry point for the consolidated workbook. Call after every
    successful Vendor Inventory import; sends ONE workbook once imports have
    been quiet for `WHATSAPP_WORKBOOK_DEBOUNCE_SECONDS` (each new request
    within the window restarts the countdown). A delay of 0 sends immediately.
    `vendor_name` (display name, never the code) is collected across the
    batch and listed in the send's caption."""
    global _debounce_timer
    delay = whatsapp_settings.workbook_debounce_seconds
    with _debounce_lock:
        if vendor_name and vendor_name.strip():
            _pending_vendor_names.add(vendor_name.strip())
    if delay <= 0:
        send_consolidated_inventory_to_founder()
        return
    with _debounce_lock:
        if _debounce_timer is not None:
            _debounce_timer.cancel()
        _debounce_timer = threading.Timer(delay, _fire_debounced_send)
        _debounce_timer.daemon = True
        _debounce_timer.start()
    logger.info(
        "Consolidated Vendor Inventory workbook send scheduled in %.0fs "
        "(coalesces with any further imports in this batch).",
        delay,
    )


def _fire_debounced_send() -> None:
    global _debounce_timer
    with _debounce_lock:
        _debounce_timer = None
    send_consolidated_inventory_to_founder()


def send_consolidated_inventory_to_founder() -> None:
    """Generate the consolidated workbook and send it to the Founder. Safe to
    call after any successful WhatsApp Vendor Inventory import; never raises."""
    # Who triggered this batch (named in the caption); cleared atomically so
    # the NEXT batch starts fresh even if this send fails.
    with _debounce_lock:
        updated_by = sorted(_pending_vendor_names, key=str.lower)
        _pending_vendor_names.clear()

    if not whatsapp_settings.send_workbook:
        logger.info(
            "WHATSAPP_SEND_WORKBOOK=false -- consolidated workbook not sent to WhatsApp "
            "(available on the web: Vendor Inventory -> Download Workbook)."
        )
        return

    recipients = whatsapp_settings.admin_phone_numbers
    if not recipients:
        logger.info(
            "WHATSAPP_ADMIN_PHONE_NUMBER not set -- skipping consolidated Vendor Inventory "
            "workbook send (import already saved; this output is optional)."
        )
        return

    # Stage 2: workbook generation.
    try:
        with get_session() as session:
            workbook = workbook_service.build_workbook(session)
            content = workbook_service.workbook_to_bytes(workbook)
        sheet_count = len(workbook.sheetnames)
        logger.info(
            "Consolidated Vendor Inventory workbook generated (%d vendor worksheet(s), %d bytes).",
            sheet_count,
            len(content),
        )
    except Exception:  # noqa: BLE001 -- generation failure must not affect the import
        logger.exception("Consolidated Vendor Inventory workbook generation failed.")
        broker.publish(
            "warning",
            "Vendor Inventory imported successfully, but Excel workbook generation failed.",
            "The database import is saved and remains the source of truth.",
        )
        return

    # Stage 3: WhatsApp delivery -- every configured admin number. The
    # caption names the vendor(s) whose files triggered this batch (display
    # names only, never codes).
    caption = "Vendor Inventory updated successfully."
    if updated_by:
        caption += f"\nUpdated by: {', '.join(updated_by)}"
    sent = False
    for to in recipients:
        delivered = outbound.send_document_safe(
            to,
            content,
            workbook_service.WORKBOOK_FILENAME,
            workbook_service.XLSX_MIME_TYPE,
            caption=caption,
        )
        sent = sent or delivered
    if sent:
        # Web-only: the workbook itself has just landed in the WhatsApp chat,
        # so mirroring "it was sent" back into that same chat is pure noise.
        broker.publish(
            "success",
            "Vendor Inventory workbook sent successfully to WhatsApp.",
            f"File: {workbook_service.WORKBOOK_FILENAME}\nVendor worksheets: {sheet_count}",
            mirror=False,
        )
    else:
        # A FAILURE to deliver is worth knowing on WhatsApp -- it means the
        # file the Founder expects never arrived.
        broker.publish(
            "warning",
            "Vendor Inventory imported successfully, but the workbook could not be sent to WhatsApp.",
            "The database import is saved and remains the source of truth. "
            "Download it from Vendor Inventory -> Download Workbook.",
        )
