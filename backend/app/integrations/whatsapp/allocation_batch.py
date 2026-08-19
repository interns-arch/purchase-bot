"""Automatic vendor selection for imported customer orders -- "Combined ZIP"
mode (the Founder's chosen workflow).

Every successfully imported customer order (WhatsApp or Gmail) is queued
here. Once order imports have been quiet for
`WHATSAPP_ALLOCATION_BATCH_DEBOUNCE_SECONDS` (same debounce idea as the
consolidated inventory workbook), the batch is processed:

  for each pending order, IN ARRIVAL ORDER:
      run the UNCHANGED automatic vendor-selection engine
      (each order's allocation commits before the next order runs, so every
      customer consumes stock through the existing reservation ledger before
      the next customer is matched -- identical semantics to clicking
      Auto-Select once per customer, in sequence)
      build that order's allocation workbook
  then send ONE consolidated Excel workbook -- one worksheet per customer
  order -- to the Founder's WhatsApp. (Originally a ZIP of per-order files,
  but WhatsApp's Cloud API rejects application/zip uploads with 400; a
  multi-sheet .xlsx delivers the same "all reports together in one file"
  and mirrors the Vendor_Inventory.xlsx shape the Founder already knows.)

Failure isolation mirrors `inventory_output.py` / `allocation_output.py`:
each order gets its own session/transaction, one failing order never blocks
the others, delivery problems never roll back an allocation, and this module
never raises into its caller. The manual Auto-Select button and its
per-order outputs are untouched.
"""

from __future__ import annotations

import io
import threading

from backend.app.integrations.whatsapp import outbound
from backend.app.integrations.whatsapp import recipients as recipients_service
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.notifications import broker
from core.db import get_session
from core.logging_setup import get_logger
from core.models import Customer, CustomerOrder
from core.services import vendor_selection_service
from core.services.rules.engine import run_automatic_vendor_selection
from core.time_utils import now_ist

XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

logger = get_logger(__name__)

_lock = threading.Lock()
_pending: list[int] = []  # order ids in arrival order -- consumption sequence
_timer: threading.Timer | None = None


def request_order_allocation(order_id: int) -> None:
    """Queue one just-imported customer order for automatic vendor selection.
    Called after the import transaction has COMMITTED. Debounced: the batch
    runs once order imports go quiet; every new order restarts the countdown."""
    if not whatsapp_settings.auto_allocation_enabled:
        return

    global _timer
    delay = whatsapp_settings.allocation_batch_debounce_seconds
    run_now: list[int] | None = None
    with _lock:
        if order_id not in _pending:
            _pending.append(order_id)
        if delay <= 0:
            run_now = list(_pending)
            _pending.clear()
        else:
            if _timer is not None:
                _timer.cancel()
            _timer = threading.Timer(delay, _process_batch)
            _timer.daemon = True
            _timer.start()
    if run_now is not None:
        _run_batch(run_now)
    else:
        logger.info(
            "Customer order %s queued for automatic vendor selection "
            "(batch runs %.0fs after the last order import).",
            order_id,
            delay,
        )


def _process_batch() -> None:
    global _timer
    with _lock:
        _timer = None
        orders = list(_pending)
        _pending.clear()
    if orders:
        _run_batch(orders)


def order_identity(order_id: int, session) -> tuple[str, str, str]:
    """(worksheet_title, caption_label, sheet_heading) for one order.

    Identity is the CUSTOMER NAME plus the order id -- so a worksheet is
    never an anonymous "Order_12":

      worksheet title : "O12 Karol Bagh"      (Excel caps titles at 31 chars)
      caption label   : "Order 12 — Karol Bagh"
      sheet heading   : "Customer Order 12 — Karol Bagh (file: kb.xlsx)"
                        written INSIDE the sheet, where nothing is truncated

    Orders whose customer could not be identified (Gmail attachments with no
    Customer Code) fall back to the order id + source file name, which still
    ties the sheet to a real file."""
    order = session.get(CustomerOrder, order_id)
    file_name = getattr(order, "file_name", None) if order is not None else None
    customer_name = None
    if order is not None and order.customer_id is not None:
        customer = session.get(Customer, order.customer_id)
        customer_name = customer.name if customer is not None else None

    if customer_name:
        title = f"O{order_id} {customer_name}"[:31]
        label = f"Order {order_id} — {customer_name}"
        heading = f"Customer Order {order_id} — {customer_name}"
    else:
        title = f"Order_{order_id}"
        label = f"Order {order_id}"
        heading = f"Customer Order {order_id}"
    if file_name:
        heading += f"  (file: {file_name})"
    return title, label, heading


def _run_batch(order_ids: list[int]) -> None:
    """Auto-select every order sequentially, then send one ZIP of reports."""
    logger.info(
        "Automatic vendor selection batch starting for %d customer order(s): %s",
        len(order_ids),
        order_ids,
    )
    reports: list[tuple[str, list]] = []  # (worksheet title, export rows)
    order_labels: list[str] = []  # 'Order 12 — Karol Bagh' per report
    headings: dict[str, str] = {}  # worksheet title -> in-sheet heading
    done: list[int] = []
    failed: list[int] = []

    for order_id in order_ids:
        try:
            # One session per order, committed on exit -- the next order's
            # comparison reads this order's reservations from the ledger.
            with get_session() as session:
                run_automatic_vendor_selection(order_id, session)
                rows = vendor_selection_service.list_selections_for_export(order_id, session)
                title, label, heading = order_identity(order_id, session)
            reports.append((title, rows))
            order_labels.append(label)
            headings[title] = heading
            done.append(order_id)
        except Exception:  # noqa: BLE001 -- one bad order must not block the batch
            logger.exception(
                "Automatic vendor selection failed for customer order %s -- "
                "continuing with the rest of the batch.",
                order_id,
            )
            failed.append(order_id)

    if not reports:
        broker.publish(
            "warning",
            "Automatic vendor selection could not produce any allocation reports.",
            f"Customer orders: {order_ids}",
        )
        return

    # Founder + purchase team (Founder's rule, 18 Aug 2026) -- see
    # `recipients.internal_file_recipients`. Read on this module's own
    # session so the whole batch uses one database entry point.
    recipients: list[str] = []
    if whatsapp_settings.send_allocation_report:
        with get_session() as session:
            recipients = recipients_service.internal_file_recipients(session)
    if not recipients:
        logger.info(
            "Allocation report not sent to WhatsApp for orders %s (send_allocation_report=%s, "
            "admin numbers configured=%s) -- allocations are saved and visible on the web.",
            order_ids,
            whatsapp_settings.send_allocation_report,
            bool(whatsapp_settings.admin_phone_numbers),
        )
        broker.publish(
            "success",
            f"Automatic vendor selection completed for {len(reports)} customer order(s).",
            "Report not sent: WHATSAPP_ADMIN_PHONE_NUMBER is not configured.",
        )
        return

    workbook = vendor_selection_service.to_batch_export_workbook(reports, headings)
    buffer = io.BytesIO()
    workbook.save(buffer)
    file_name = f"vendor_allocations_{now_ist().strftime('%Y%m%d_%H%M')}.xlsx"

    detail = "\n".join(order_labels) if order_labels else f"Orders: {done}"
    if failed:
        detail += f"\nFailed (see logs): {failed}"
    caption = (
        f"Automatic vendor allocation completed for {len(reports)} customer "
        f"order(s). One worksheet per order inside."
    )
    if order_labels:
        caption += "\n" + "\n".join(order_labels)
    sent = False
    for to in recipients:
        delivered = outbound.send_document_safe(
            to,
            buffer.getvalue(),
            file_name,
            XLSX_MIME_TYPE,
            caption=caption,
        )
        sent = sent or delivered
    if sent:
        # Web-only: the allocation workbook itself just arrived in the chat
        # (with the customer names in its caption) -- a second "it was sent"
        # message there would just be noise.
        broker.publish(
            "success",
            f"Automatic vendor selection completed -- {file_name} sent to WhatsApp.",
            detail,
            mirror=False,
        )
    else:
        broker.publish(
            "warning",
            "Automatic vendor selection completed, but the report could not be sent to WhatsApp.",
            detail + "\nAllocations are saved; use the Vendor Comparison export if needed.",
        )
