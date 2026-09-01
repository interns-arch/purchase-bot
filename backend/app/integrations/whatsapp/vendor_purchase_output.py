"""VENDOR-WISE PURCHASE OUTPUT after an allocation batch.

Both outputs here answer one question -- **what do we buy from this
vendor?** -- and the batch's allocations are regrouped BY VENDOR to do it.

CURRENT PRIMARY OUTPUT (Founder, 1 Sep 2026): ONE EXCEL, ONE SHEET PER
VENDOR. A `Summary` tab indexes the vendors, then one tab per vendor named
after that vendor ("Bijwasan", "Northend", "Jaipur"), each listing:

    Part Number | Vendor Part Number | Customer Requested |
    Vendor Available | Allocated Qty | Status | Customer Order

That detail is the reason for the change: a text message can only carry
"part x quantity", while the sheet also shows what the customer asked for
and what the vendor actually had -- so a Partial allocation explains itself.
Controlled by `WHATSAPP_VENDOR_WORKBOOK` (default true).

PREVIOUS OUTPUT, still available and used as the FALLBACK -- one text
message per vendor (Founder, 25 Aug 2026, on the combined per-order
workbook: "This sheet is difficult to understand. Rather it should give me
separate whatsapp. E.g. Purchase from Ess aay ... / Purchase from Northend
/ Purchase from Bijwasan / Purchase from Jaipur"):

    🛒 Purchase from BIJWASHAN STOCK
    3 part(s) · total qty 713
    For: Order 65 - Karol Bagh

    1283169G10 x 53
    0928348007 x 660
    4243154P00 x 130

Set `WHATSAPP_VENDOR_WORKBOOK=false` to go back to it permanently. It is
also used automatically if the workbook cannot be built or delivered -- the
purchase instructions matter more than their format, so a spreadsheet
problem must never leave the Founder with nothing.

A vendor with more lines than `WHATSAPP_VENDOR_MESSAGE_MAX_LINES` gets a
small single-vendor Excel instead of an unreadable wall of text -- the
"details in excel or text" the Founder asked for, chosen by size.

BOTH go to the INTERNAL recipients only (founder/admin number(s) + the
registered purchase team -- `recipients.internal_file_recipients`). A vendor
never receives either one: they list every vendor we buy from and at what
quantity. The vendor's own document is the Purchase Order (`po_output`).

Never raises into the caller: a delivery failure must not affect an
allocation that is already committed.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
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


@dataclass
class PurchaseLine:
    """One "buy this part from this vendor" line, with the context needed to
    justify it: what the customer asked for, what the vendor had, and what we
    took."""

    part_number: str
    vendor_part_number: str
    requested: Decimal | None
    available: Decimal | None
    quantity: Decimal
    status: str
    order_label: str


def _quantity(value) -> Decimal:
    return Decimal(str(value)) if value is not None else Decimal(0)


def _optional_quantity(value) -> Decimal | None:
    """Like `_quantity`, but keeps "not known" distinct from zero -- an
    available column showing a blank means we never had a figure, which is
    not the same as the vendor having none."""
    return Decimal(str(value)) if value is not None else None


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
    """{vendor_name: {"lines": [PurchaseLine], "qty": Decimal}} built from the
    batch's per-order export rows. Orders are remembered per line so one
    vendor's message can say which customers it covers.

    Every field of the export row is carried through -- not just part and
    quantity -- because the workbook shows what the customer REQUESTED and
    what the vendor had AVAILABLE next to what we allocated. The text
    message uses only `part_number` and `quantity`."""
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
                PurchaseLine(
                    part_number=getattr(row, "customer_part_number", "") or "",
                    vendor_part_number=getattr(row, "vendor_part_number", "") or "",
                    requested=_optional_quantity(getattr(row, "requested_quantity", None)),
                    available=_optional_quantity(getattr(row, "available_quantity", None)),
                    quantity=quantity,
                    status=(getattr(row, "status", "") or "").strip(),
                    order_label=order_label,
                )
            )
            entry["qty"] += quantity
    return by_vendor


def build_message(vendor: str, entry: dict, *, max_lines: int) -> tuple[str, bool]:
    """(message_text, needs_excel). The text always summarises; whether the
    individual lines are included depends on how many there are."""
    lines = entry["lines"]
    orders = []
    for line in lines:
        if line.order_label not in orders:
            orders.append(line.order_label)

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

    body = [f"{line.part_number} x {_tidy_number(line.quantity)}" for line in lines]
    return "\n".join(header + [""] + body), False


def _safe_file_part(vendor: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", vendor).strip("_")[:40] or "vendor"


def build_workbook(vendor: str, entry: dict) -> bytes:
    """One small sheet listing this vendor's purchase lines -- used when a
    SINGLE vendor's text message is too long. The multi-vendor workbook is
    `build_vendor_workbook`."""
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = _sheet_title(vendor, set())
    _fill_vendor_sheet(sheet, vendor, entry)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# The columns the Founder asked for: the part, what the customer wanted,
# what this vendor had, and what we are actually buying.
VENDOR_SHEET_HEADERS = (
    "Part Number",
    "Vendor Part Number",
    "Customer Requested",
    "Vendor Available",
    "Allocated Qty",
    "Status",
    "Customer Order",
)


def _sheet_title(vendor: str, used: set[str]) -> str:
    """A legal, unique Excel worksheet title for this vendor.

    Excel caps a tab at 31 characters and forbids : \\ / ? * [ ] -- and a
    duplicate title raises. Vendor names routinely exceed 31 characters
    ("BIJWASHAN STOCK PVT LTD - DELHI"), so this truncates and then
    de-duplicates."""
    base = re.sub(r"[:\\/?*\[\]]", "_", (vendor or "Vendor").strip())[:31] or "Vendor"
    candidate, suffix = base, 2
    while candidate.lower() in used:
        candidate = f"{base[:28]}_{suffix}"
        suffix += 1
    used.add(candidate.lower())
    return candidate


def _fill_vendor_sheet(sheet, vendor: str, entry: dict) -> None:
    """One vendor's purchase lines, with the vendor's full name as an
    in-sheet heading (the tab caps at 31 characters, so a long name can only
    be read here)."""
    from openpyxl.styles import Font

    lines = entry["lines"]
    sheet.append([f"Purchase from {vendor}"])
    sheet["A1"].font = Font(bold=True, size=12)
    sheet.append(
        [f"{len(lines)} part(s) · total qty {_tidy_number(entry['qty'])}"]
    )
    sheet.append([])

    header_index = sheet.max_row + 1
    sheet.append(list(VENDOR_SHEET_HEADERS))
    for cell in sheet[header_index]:
        cell.font = Font(bold=True)

    for line in lines:
        sheet.append(
            [
                line.part_number,
                line.vendor_part_number,
                float(line.requested) if line.requested is not None else None,
                float(line.available) if line.available is not None else None,
                float(line.quantity),
                line.status,
                line.order_label,
            ]
        )

    for column_cells in sheet.columns:
        width = max(len(str(cell.value)) if cell.value is not None else 0
                    for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(width + 2, 46)


def _fill_summary_sheet(sheet, by_vendor: dict, tab_by_vendor: dict[str, str]) -> None:
    """A leading index: which vendors we are buying from, how much from each,
    and which tab to open. With a dozen vendor tabs this is what makes the
    workbook readable at a glance."""
    from openpyxl.styles import Font

    sheet.append(["Purchase Summary"])
    sheet["A1"].font = Font(bold=True, size=12)
    sheet.append([f"Generated {now_ist().strftime('%d %b %Y %H:%M')} IST"])
    sheet.append([])

    header_index = sheet.max_row + 1
    sheet.append(["Vendor", "Parts", "Total Qty", "Customer Orders", "Sheet"])
    for cell in sheet[header_index]:
        cell.font = Font(bold=True)

    for vendor, entry in _vendors_by_size(by_vendor):
        orders: list[str] = []
        for line in entry["lines"]:
            if line.order_label not in orders:
                orders.append(line.order_label)
        sheet.append(
            [
                vendor,
                len(entry["lines"]),
                float(entry["qty"]),
                ", ".join(orders),
                tab_by_vendor.get(vendor, ""),
            ]
        )

    for column_cells in sheet.columns:
        width = max(len(str(cell.value)) if cell.value is not None else 0
                    for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = min(width + 2, 46)


def _vendors_by_size(by_vendor: dict) -> list[tuple[str, dict]]:
    """Biggest purchase first -- the vendor with most to buy leads."""
    return sorted(by_vendor.items(), key=lambda item: item[1]["qty"], reverse=True)


def build_vendor_workbook(by_vendor: dict) -> bytes:
    """ONE workbook, ONE SHEET PER VENDOR (the Founder's ask).

    A Summary tab first, then a tab per vendor named after that vendor --
    "Bijwasan", "Northend", "Jaipur" -- each listing that vendor's parts with
    the customer's requested quantity, the vendor's available quantity and
    the quantity allocated.

    This is internal: it shows every vendor we buy from and at what
    quantities, so it goes to the Founder and the purchase team only, never
    to a vendor."""
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)

    used: set[str] = set()
    tab_by_vendor: dict[str, str] = {}
    for vendor, _entry in _vendors_by_size(by_vendor):
        tab_by_vendor[vendor] = _sheet_title(vendor, used)

    _fill_summary_sheet(workbook.create_sheet(title="Summary"), by_vendor, tab_by_vendor)
    for vendor, entry in _vendors_by_size(by_vendor):
        _fill_vendor_sheet(workbook.create_sheet(title=tab_by_vendor[vendor]), vendor, entry)

    if not workbook.sheetnames:  # an empty workbook is invalid
        workbook.create_sheet(title="No Purchases")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_workbook_caption(by_vendor: dict) -> str:
    """The WhatsApp caption sent with the workbook -- the same at-a-glance
    summary the per-vendor messages gave, in one message."""
    vendors = _vendors_by_size(by_vendor)
    total_qty = sum((entry["qty"] for _v, entry in vendors), Decimal(0))
    total_lines = sum(len(entry["lines"]) for _v, entry in vendors)

    orders: list[str] = []
    for _vendor, entry in vendors:
        for line in entry["lines"]:
            if line.order_label not in orders:
                orders.append(line.order_label)

    header = [
        "🛒 Purchase plan by vendor",
        f"{len(vendors)} vendor(s) · {total_lines} part(s) · total qty "
        f"{_tidy_number(total_qty)}",
    ]
    if orders:
        header.append("For: " + ", ".join(orders))
    header.append("")
    header.append("One sheet per vendor inside:")
    for vendor, entry in vendors:
        header.append(
            f"• {vendor} — {len(entry['lines'])} part(s), qty "
            f"{_tidy_number(entry['qty'])}"
        )
    return "\n".join(header)


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

        stamp = now_ist().strftime("%Y%m%d_%H%M")

        # PRIMARY OUTPUT (Founder, 1 Sep 2026): ONE Excel with one sheet per
        # vendor, rather than one text message per vendor. Each sheet carries
        # the detail a text cannot -- customer requested and vendor available
        # alongside the allocated quantity. Set WHATSAPP_VENDOR_WORKBOOK=false
        # to go back to the per-vendor text messages.
        if whatsapp_settings.vendor_workbook:
            return _send_vendor_workbook(by_vendor, recipients, stamp)

        return _send_vendor_texts(by_vendor, recipients, stamp)
    except Exception:  # noqa: BLE001 -- an output must never affect an allocation
        logger.exception("Vendor-wise purchase messages failed (allocation unaffected).")
        return 0


def _send_vendor_workbook(by_vendor: dict, recipients: list[str], stamp: str) -> int:
    """Send ONE workbook with a sheet per vendor to the internal recipients.
    Returns the vendor count when it was delivered, else 0.

    If the workbook cannot be built or delivered, this FALLS BACK to the
    per-vendor text messages rather than leaving the Founder with nothing --
    the purchase instructions matter more than their format."""
    try:
        content = build_vendor_workbook(by_vendor)
        caption = build_workbook_caption(by_vendor)
        file_name = f"purchase_by_vendor_{stamp}.xlsx"
    except Exception:  # noqa: BLE001
        logger.exception(
            "Could not build the vendor-wise purchase workbook -- falling back to "
            "per-vendor text messages."
        )
        return _send_vendor_texts(by_vendor, recipients, stamp)

    delivered = False
    for to in recipients:
        delivered = (
            outbound.send_document_safe(
                to, content, file_name, XLSX_MIME_TYPE, caption
            )
            or delivered
        )

    if not delivered:
        logger.warning(
            "The vendor-wise purchase workbook could not be delivered -- falling "
            "back to per-vendor text messages."
        )
        return _send_vendor_texts(by_vendor, recipients, stamp)

    logger.info(
        "Vendor-wise purchase workbook sent (%d vendor sheet(s), %d recipient(s)): %s",
        len(by_vendor),
        len(recipients),
        file_name,
    )
    return len(by_vendor)


def _send_vendor_texts(by_vendor: dict, recipients: list[str], stamp: str) -> int:
    """The original one-message-per-vendor output (Founder, 25 Aug 2026),
    kept as both the `WHATSAPP_VENDOR_WORKBOOK=false` behaviour and the
    fallback when the workbook cannot be sent."""
    max_lines = whatsapp_settings.vendor_message_max_lines
    sent_vendors = 0

    # Biggest purchase first: the vendor with most to buy leads the chat.
    for vendor, entry in _vendors_by_size(by_vendor):
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
            logger.exception("Could not send the purchase message for vendor %r.", vendor)
            continue
        if delivered:
            sent_vendors += 1

    logger.info(
        "Vendor-wise purchase messages sent for %d of %d vendor(s).",
        sent_vendors,
        len(by_vendor),
    )
    return sent_vendors
