"""ONE WHATSAPP MESSAGE PER VENDOR after an allocation batch.

The Founder's instruction (25 Aug 2026), on seeing the combined workbook:

    "This sheet is difficult to understand. Rather it should give me
     separate whatsapp. E.g. Purchase from Ess aay: then details in excel
     or text / Purchase from Northend / Purchase from Bijwasan / Purchase
     from Jaipur"

So instead of one `vendor_allocations_*.xlsx` covering every order, the
batch's allocations are regrouped BY VENDOR and each vendor gets its own
message answering one question: **what do we buy from this vendor?**

    🛒 Purchase from BIJWASHAN STOCK
    3 part(s) · total qty 713
    For: Order 65 - Karol Bagh

    1283169G10 x 53
    0928348007 x 660
    4243154P00 x 130

A vendor with more lines than `WHATSAPP_VENDOR_MESSAGE_MAX_LINES` gets a
small Excel for that vendor instead of an unreadable wall of text -- the
"details in excel or text" the Founder asked for, chosen by size.

These go to the INTERNAL recipients only (Founder + purchase team), exactly
like the workbook they replace. A vendor never receives this: it lists what
we buy, and the vendor's own document is the Purchase Order (`po_output`).

Never raises into the caller: a delivery failure must not affect an
allocation that is already committed.
"""

from __future__ import annotations

import io
import re
from decimal import Decimal

from backend.app.integrations.whatsapp import outbound
from backend.app.integrations.whatsapp import recipients as recipients_service
from backend.app.integrations.whatsapp.config import whatsapp_settings
from core.logging_setup import get_logger
from core.time_utils import now_ist

logger = get_logger(__name__)

XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Only these count as "we are buying this from them". A line nobody can
# supply (Cannot Fulfill) or one still awaiting a choice (Not Selected)
# belongs in the shortage report, not in a purchase instruction.
_PURCHASED_STATUSES = {"fulfilled", "partial"}


def _quantity(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _tidy_number(value: Decimal) -> str:
    """1200.0000 -> '1200', 2.5000 -> '2.5' (quantities read naturally).

    The decimal point check is essential: stripping trailing zeros from a
    whole number turns 660 into 66 and 130 into 13 -- i.e. it would tell the
    Founder to buy a tenth of what was actually allocated."""
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def group_by_vendor(reports: list[tuple[str, list]], labels: dict[str, str]) -> dict:
    """{vendor_name: {"lines": [(part, qty, order_label)], "qty": Decimal}}
    built from the batch's per-order export rows. Orders are remembered per
    line so one vendor's message can say which customers it covers."""
    by_vendor: dict[str, dict] = {}
    for title, rows in reports:
        order_label = labels.get(title, title)
        for row in rows:
            status = (getattr(row, "status", "") or "").strip().lower()
            if status not in _PURCHASED_STATUSES:
                continue
            vendor = (getattr(row, "vendor_name", "") or "").strip()
            quantity = _quantity(getattr(row, "selected_quantity", None))
            if not vendor or quantity <= 0:
                continue
            entry = by_vendor.setdefault(vendor, {"lines": [], "qty": Decimal(0)})
            entry["lines"].append(
                (
                    getattr(row, "customer_part_number", "") or "",
                    quantity,
                    order_label,
                )
            )
            entry["qty"] += quantity
    return by_vendor


def build_message(vendor: str, entry: dict, *, max_lines: int) -> tuple[str, bool]:
    """(message_text, needs_excel). The text always summarises; whether the
    individual lines are included depends on how many there are."""
    lines = entry["lines"]
    orders = []
    for _part, _qty, order_label in lines:
        if order_label not in orders:
            orders.append(order_label)

    header = [
        f"🛒 Purchase from {vendor}",
        f"{len(lines)} part(s) · total qty {_tidy_number(entry['qty'])}",
        "For: " + ", ".join(orders) if orders else "",
    ]
    header = [line for line in header if line]

    if len(lines) > max_lines:
        header.append("")
        header.append("Full list attached (too many lines for one message).")
        return "\n".join(header), True

    body = [f"{part} x {_tidy_number(qty)}" for part, qty, _order in lines]
    return "\n".join(header + [""] + body), False


def _safe_file_part(vendor: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", vendor).strip("_")[:40] or "vendor"


def build_workbook(vendor: str, entry: dict) -> bytes:
    """One small sheet listing this vendor's purchase lines."""
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = _safe_file_part(vendor)[:31] or "Purchase"
    sheet.append(["Part Number", "Quantity", "Customer Order"])
    for part, quantity, order_label in entry["lines"]:
        sheet.append([part, float(quantity), order_label])
    sheet.column_dimensions["A"].width = 22
    sheet.column_dimensions["B"].width = 12
    sheet.column_dimensions["C"].width = 34
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def send_vendor_messages(
    reports: list[tuple[str, list]], labels: dict[str, str]
) -> int:
    """Send one message per vendor. Returns how many vendors were messaged.
    Never raises."""
    try:
        if not whatsapp_settings.allocation_per_vendor:
            return 0

        by_vendor = group_by_vendor(reports, labels)
        if not by_vendor:
            logger.info(
                "Vendor-wise purchase messages: nothing allocated in this batch "
                "(only shortages/unselected lines) -- nothing to send."
            )
            return 0

        recipients = recipients_service.internal_file_recipients()
        if not recipients:
            logger.info(
                "Vendor-wise purchase messages: no internal recipients configured."
            )
            return 0

        max_lines = whatsapp_settings.vendor_message_max_lines
        stamp = now_ist().strftime("%Y%m%d_%H%M")
        sent_vendors = 0

        # Biggest purchase first: the vendor with most to buy leads the chat.
        for vendor, entry in sorted(
            by_vendor.items(), key=lambda item: item[1]["qty"], reverse=True
        ):
            message, needs_excel = build_message(vendor, entry, max_lines=max_lines)
            delivered = False
            try:
                if needs_excel:
                    content = build_workbook(vendor, entry)
                    file_name = f"purchase_{_safe_file_part(vendor)}_{stamp}.xlsx"
                    for to in recipients:
                        delivered = (
                            outbound.send_document_safe(
                                to, content, file_name, XLSX_MIME_TYPE, message
                            )
                            or delivered
                        )
                else:
                    for to in recipients:
                        delivered = outbound.send_reply_safe(to, message) or delivered
            except Exception:  # noqa: BLE001 -- one vendor must not stop the rest
                logger.exception(
                    "Could not send the purchase message for vendor %r.", vendor
                )
                continue
            if delivered:
                sent_vendors += 1

        logger.info(
            "Vendor-wise purchase messages sent for %d of %d vendor(s).",
            sent_vendors,
            len(by_vendor),
        )
        return sent_vendors
    except Exception:  # noqa: BLE001 -- an output must never affect an allocation
        logger.exception("Vendor-wise purchase messages failed (allocation unaffected).")
        return 0
