"""Deliver the auto top-up result as an EXCEL REPORT, in exactly the same
shape as the automatic allocation report -- one worksheet per affected
customer order, each headed with that order's identity.

A long text message listing every part is hard to read on a phone; the
workbook is the format the Founder already knows from
`vendor_allocations_*.xlsx`. The file is named `vendor_reallocation_*.xlsx`
so the two are never confused: "allocation" = a new order was matched,
"reallocation" = new stock filled gaps in orders that were already here.

Each worksheet shows the order's COMPLETE current allocation (what was
already allocated plus what this new stock just added), so the sheet can be
acted on directly without cross-referencing the earlier report.

Best-effort: the top-up itself is already committed before this runs, and
no failure here can affect it."""

from __future__ import annotations

import io

from backend.app.integrations.whatsapp import outbound
from backend.app.integrations.whatsapp.allocation_batch import order_identity
from backend.app.integrations.whatsapp.config import whatsapp_settings
from core.db import get_session
from core.logging_setup import get_logger
from core.services import vendor_selection_service
from core.time_utils import now_ist

logger = get_logger(__name__)

XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_reallocation_workbook(order_ids: list[int], session):
    """One worksheet per affected order, full current allocation inside.
    Returns (workbook, {order_id: caption label}) or (None, {}) when nothing
    could be built."""
    reports: list[tuple[str, list]] = []
    headings: dict[str, str] = {}
    labels: dict[int, str] = {}
    for order_id in order_ids:
        rows = vendor_selection_service.list_selections_for_export(order_id, session)
        if not rows:
            continue
        title, label, heading = order_identity(order_id, session)
        reports.append((title, rows))
        headings[title] = f"{heading}  — updated from new vendor stock"
        labels[order_id] = label
    if not reports:
        return None, {}
    return vendor_selection_service.to_batch_export_workbook(reports, headings), labels


def send_topup_report(result) -> bool:
    """Send the reallocation workbook for a completed top-up. Returns True if
    at least one send succeeded. Never raises."""
    try:
        if not result.lines:
            return False
        if not whatsapp_settings.send_allocation_report:
            logger.info(
                "WHATSAPP_SEND_ALLOCATION_REPORT=false -- reallocation workbook not sent."
            )
            return False
        recipients = whatsapp_settings.admin_phone_numbers
        if not recipients:
            return False

        with get_session() as session:
            workbook, labels = build_reallocation_workbook(result.order_ids, session)
        if workbook is None:
            return False

        buffer = io.BytesIO()
        workbook.save(buffer)
        file_name = f"vendor_reallocation_{now_ist().strftime('%Y%m%d_%H%M')}.xlsx"

        vendor = result.vendor_name or "new stock"
        caption_lines = [
            f"🔄 New stock from {vendor} filled gaps in "
            f"{len(result.order_ids)} existing order(s). "
            "One worksheet per order inside; existing allocations were not changed."
        ]
        caption_lines += [labels.get(order_id, f"Order {order_id}") for order_id in result.order_ids]
        caption = "\n".join(caption_lines)

        sent = False
        for to in recipients:
            sent = (
                outbound.send_document_safe(
                    to, buffer.getvalue(), file_name, XLSX_MIME_TYPE, caption
                )
                or sent
            )
        if sent:
            logger.info("Reallocation workbook %s sent (%s order(s)).", file_name, len(labels))
        return sent
    except Exception:  # noqa: BLE001 -- an output must never affect the top-up
        logger.exception("Could not send the reallocation workbook.")
        return False
