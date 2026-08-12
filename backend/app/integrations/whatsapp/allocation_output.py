"""Per-customer-order allocation report over WhatsApp.

After Automatic Vendor Selection finishes for a customer order, the SAME
allocation workbook the UI export produces (`vendor_selection_service.
list_selections_for_export` + `to_export_workbook` -- one row per selected
vendor with Requested/Available/Selected quantities and a Fulfilled/Partial/
Cannot-Fulfill status) is sent to the Founder's WhatsApp number.

This mirrors `inventory_output.py` exactly:
- best-effort and failure-isolated: the allocation is already committed before
  this runs, opens its OWN session, and never raises -- a delivery failure can
  never change or roll back a vendor selection;
- skipped cleanly when `WHATSAPP_ADMIN_PHONE_NUMBER` is unset;
- reuses the existing WhatsApp client/credentials -- no new auth.

Because each customer order gets its own report generated from the database
AT THAT MOMENT, Customer B's report automatically reflects the inventory
state after Customer A's allocation -- the workbook is a view of the
reservation ledger, never a source of truth.
"""

from __future__ import annotations

import io

from backend.app.integrations.whatsapp import outbound
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.notifications import broker
from core.db import get_session
from core.logging_setup import get_logger
from core.services import vendor_selection_service

XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


logger = get_logger(__name__)


def send_allocation_report_to_founder(order_id: int) -> None:
    """Generate this order's allocation workbook from the database and send it
    to the Founder over WhatsApp. Safe to call after auto-select; never raises."""
    recipients = whatsapp_settings.admin_phone_numbers
    if not recipients:
        logger.info(
            "WHATSAPP_ADMIN_PHONE_NUMBER not set -- skipping WhatsApp allocation "
            "report for order %s (allocation already saved; this output is optional).",
            order_id,
        )
        return

    file_name = f"vendor_allocation_order_{order_id}.xlsx"
    try:
        with get_session() as session:
            rows = vendor_selection_service.list_selections_for_export(order_id, session)
            workbook = vendor_selection_service.to_export_workbook(rows)
        buffer = io.BytesIO()
        workbook.save(buffer)
        content = buffer.getvalue()
        row_count = len(rows)
    except Exception:  # noqa: BLE001 -- report generation must not affect the allocation
        logger.exception("Could not build the allocation workbook for order %s.", order_id)
        broker.publish(
            "warning",
            "Vendor allocation saved, but the report workbook could not be generated.",
            f"Customer Order: {order_id}",
        )
        return

    sent = False
    for to in recipients:
        delivered = outbound.send_document_safe(
            to,
            content,
            file_name,
            XLSX_MIME_TYPE,
            caption=f"Vendor allocation report for Customer Order {order_id}.",
        )
        sent = sent or delivered
    if sent:
        broker.publish(
            "success",
            "Vendor allocation report sent to WhatsApp.",
            f"Customer Order: {order_id}\nReport rows: {row_count}",
        )
    else:
        broker.publish(
            "warning",
            "Vendor allocation saved, but the report could not be sent to WhatsApp.",
            f"Customer Order: {order_id}",
        )
