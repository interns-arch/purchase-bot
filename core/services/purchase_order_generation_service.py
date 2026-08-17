"""Purchase Order generation for the web app: after Vendor Selection
(manual or automatic) allocates one or more vendors to a customer order,
this groups those allocations vendor-wise into one Purchase Order per
vendor. Deliberately named and modeled apart from the legacy CLI's
`core.services.purchase_order_service` (kept untouched, PO-based matching
pipeline) -- this one is keyed off `VendorSelection`, the web app's own
allocation model.

Purchase Orders here are never sent to a vendor -- only ever emailed
internally to `PURCHASE_TEAM_EMAIL` for review (see
`generate_and_email_purchase_orders`); vendor-direct delivery is deferred to
a future phase.

Pure business logic -- no FastAPI/print()/input() here, except for reading
`backend.app.core.config.settings` for company details/email destination
(mirrors how `google_sheets_sync_service.py` reads its own settings module).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import openpyxl
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.ingestion.column_detector import decimal_to_string
from core.logging_setup import get_logger
from core.time_utils import now_ist
from core.models import (
    CustomerOrder,
    Part,
    Vendor,
    VendorPurchaseOrder,
    VendorPurchaseOrderItem,
    VendorPurchaseOrderStatus,
    VendorSelection,
)
from core.services import vendor_selection_service

logger = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _po_number(order_id: int, vendor: Vendor) -> str:
    return f"PO-{order_id}-{vendor.vendor_code or f'V{vendor.id}'}"


def _clear_existing_purchase_orders(order_id: int, session: Session) -> None:
    """Re-running Automatic Vendor Selection replaces the previous
    allocation (see `vendor_selection_service.clear_selections_for_item`) --
    any Purchase Orders generated from the old allocation are stale and are
    replaced the same way, rather than accumulating duplicates."""
    existing = list(
        session.execute(
            select(VendorPurchaseOrder).where(VendorPurchaseOrder.customer_order_id == order_id)
        ).scalars()
    )
    for po in existing:
        session.delete(po)
    session.flush()


def generate_purchase_orders_for_order(
    order_id: int, session: Session
) -> list[VendorPurchaseOrder]:
    """Reads the customer order's current `VendorSelection` rows (unchanged
    -- reused via `vendor_selection_service.list_selections_for_order`),
    groups them by vendor, and creates one `VendorPurchaseOrder` + item rows
    per vendor. Returns the newly created POs."""
    _clear_existing_purchase_orders(order_id, session)

    selections = vendor_selection_service.list_selections_for_order(order_id, session)
    if not selections:
        return []

    selections_by_vendor: dict[int, list[VendorSelection]] = {}
    for selection in selections:
        selections_by_vendor.setdefault(selection.vendor_id, []).append(selection)

    purchase_orders: list[VendorPurchaseOrder] = []
    for vendor_id, vendor_selections in selections_by_vendor.items():
        vendor = session.get(Vendor, vendor_id)

        po = VendorPurchaseOrder(
            po_number=_po_number(order_id, vendor),
            vendor_id=vendor_id,
            customer_order_id=order_id,
            status=VendorPurchaseOrderStatus.GENERATED,
        )
        session.add(po)
        session.flush()  # assign po.id

        for selection in vendor_selections:
            session.add(
                VendorPurchaseOrderItem(
                    purchase_order_id=po.id,
                    part_id=selection.part_id,
                    vendor_part_number=selection.vendor_part_number,
                    quantity=selection.quantity_selected,
                    customer_order_item_id=selection.customer_order_item_id,
                )
            )

        purchase_orders.append(po)

    session.flush()
    return purchase_orders


@dataclass
class PurchaseOrderLine:
    part_number: str
    vendor_part_number: str
    quantity: Decimal


EXPORT_HEADERS = [
    "Vendor Name",
    "Vendor Code",
    "Customer Order Reference",
    "Part Number",
    "Vendor Part Number",
    "Quantity",
    "Date",
]


def list_purchase_order_lines(po_id: int, session: Session) -> list[PurchaseOrderLine]:
    items = list(
        session.execute(
            select(VendorPurchaseOrderItem).where(
                VendorPurchaseOrderItem.purchase_order_id == po_id
            )
        ).scalars()
    )
    lines = []
    for item in items:
        part = session.get(Part, item.part_id)
        lines.append(
            PurchaseOrderLine(
                part_number=part.canonical_part_number if part else str(item.part_id),
                vendor_part_number=item.vendor_part_number,
                quantity=item.quantity,
            )
        )
    return lines


def to_export_workbook(
    po: VendorPurchaseOrder,
    lines: list[PurchaseOrderLine],
    *,
    company_name: str,
    company_address: str,
    company_phone: str,
    company_email: str,
) -> openpyxl.Workbook:
    """Purchase Order export -- same bold-header/autosize pattern as every
    other export in this app (`vendor_selection_service.to_export_workbook`,
    `vendor_comparison_service.to_workbook`)."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Purchase Order"

    sheet.append([f"Purchase Order: {po.po_number}"])
    sheet.append([f"Company: {company_name}"])
    sheet.append([f"Address: {company_address}"])
    sheet.append([f"Phone: {company_phone}  Email: {company_email}"])
    sheet.append([f"Vendor: {po.vendor.name} ({po.vendor.vendor_code or '-'})"])
    sheet.append([f"Customer Order Reference: {po.customer_order_id}"])
    sheet.append([f"Date: {now_ist().date().isoformat()}"])
    sheet.append([])

    header_row_index = sheet.max_row + 1
    sheet.append(EXPORT_HEADERS)
    for cell in sheet[header_row_index]:
        cell.font = Font(bold=True)

    for line in lines:
        sheet.append(
            [
                po.vendor.name,
                po.vendor.vendor_code or "-",
                po.customer_order_id,
                line.part_number,
                line.vendor_part_number,
                decimal_to_string(line.quantity),
                now_ist().date().isoformat(),
            ]
        )

    for column_cells in sheet.columns:
        max_length = max((len(str(cell.value)) for cell in column_cells if cell.value), default=10)
        sheet.column_dimensions[column_cells[0].column_letter].width = max_length + 2

    return workbook


def list_purchase_orders(session: Session, *, order_id: int | None = None) -> list[VendorPurchaseOrder]:
    statement = select(VendorPurchaseOrder).order_by(VendorPurchaseOrder.created_at.desc())
    if order_id is not None:
        statement = statement.where(VendorPurchaseOrder.customer_order_id == order_id)
    return list(session.execute(statement).scalars())


def get_purchase_order(po_id: int, session: Session) -> VendorPurchaseOrder | None:
    return session.get(VendorPurchaseOrder, po_id)


def _email_purchase_order(
    po: VendorPurchaseOrder, session: Session, *, order: CustomerOrder | None = None
) -> bool:
    """Build one PO's workbook and email it to the internal purchase team
    (`PURCHASE_TEAM_EMAIL`), updating THIS PO's own `status`/`emailed_at`.
    Each PO tracks its own email outcome independently -- if some POs email
    and others fail, each carries its own EMAILED / EMAIL_FAILED status.
    Never raises: a mail failure is logged and recorded as EMAIL_FAILED.
    Returns True only on a confirmed successful send.

    Shared by both the automatic post-selection email and the manual
    "Resend Email" action, so the two can never drift apart. The caller is
    responsible for the enable/destination gating and the surrounding
    `session.flush()`."""
    # Imported lazily to avoid a routing-layer import at module load time --
    # `backend.app.core.config`/`backend.app.integrations.gmail.mailer` live
    # above `core/`, and this keeps `core/services/*` importable standalone
    # (e.g. from the CLI scripts) without requiring the backend package.
    from backend.app.core.config import settings
    from backend.app.integrations.gmail.mailer import send_email

    if order is None:
        order = session.get(CustomerOrder, po.customer_order_id)

    lines = list_purchase_order_lines(po.id, session)
    workbook = to_export_workbook(
        po,
        lines,
        company_name=settings.company_name,
        company_address=settings.company_address,
        company_phone=settings.company_phone,
        company_email=settings.company_email,
    )
    buffer = io.BytesIO()
    workbook.save(buffer)

    body = (
        f"Purchase Order: {po.po_number}\n"
        f"Vendor: {po.vendor.name} ({po.vendor.vendor_code or '-'})\n"
        f"Customer Order Reference: {order.file_name if order else po.customer_order_id}\n"
        f"Date: {now_ist().date().isoformat()}\n"
    )

    try:
        success = send_email(
            settings.purchase_team_email,
            f"Purchase Order {po.po_number} — for internal review",
            body,
            attachments=[(f"{po.po_number}.xlsx", buffer.getvalue())],
        )
        po.status = (
            VendorPurchaseOrderStatus.EMAILED
            if success
            else VendorPurchaseOrderStatus.EMAIL_FAILED
        )
        if success:
            po.emailed_at = _utcnow()
        return success
    except Exception:  # noqa: BLE001 -- a mail failure must never fail the caller
        logger.exception("Failed to email purchase order %s", po.po_number)
        po.status = VendorPurchaseOrderStatus.EMAIL_FAILED
        return False


def generate_and_email_purchase_orders(order_id: int, session: Session) -> list[VendorPurchaseOrder]:
    """Generates Purchase Orders for this order, then -- if
    `ENABLE_PO_EMAIL` and `PURCHASE_TEAM_EMAIL` are configured -- emails each
    one's workbook to the internal purchase team only (never to a vendor).
    A mail failure is recorded on that PO's own status but never raised; PO
    generation itself always succeeds regardless of email outcome, and each
    PO keeps its own independent email status."""
    from backend.app.core.config import settings

    purchase_orders = generate_purchase_orders_for_order(order_id, session)
    if not purchase_orders:
        return []

    if settings.enable_po_email and settings.purchase_team_email:
        order = session.get(CustomerOrder, order_id)
        for po in purchase_orders:
            _email_purchase_order(po, session, order=order)
        session.flush()

    # WhatsApp distribution (Founder's rule: vendor + purchase team + order
    # sender + Founder, each vendor receiving ONLY their own PO). Reads
    # through THIS session (same pattern as the email above) and can never
    # affect PO generation or the email outcome.
    session.flush()
    try:
        from backend.app.integrations.whatsapp import po_output

        for po in purchase_orders:
            po_output.send_po_on_whatsapp(po.id, session)
    except Exception:  # noqa: BLE001 -- delivery must never affect generation
        logger.exception("WhatsApp PO distribution failed (POs are generated and saved).")

    return purchase_orders


def resend_purchase_order_email(po_id: int, session: Session) -> VendorPurchaseOrder | None:
    """Manually re-send one Purchase Order's internal email (the UI "Resend
    Email" action), e.g. after a transient SMTP outage caused EMAIL_FAILED.
    Updates only that PO's own status. Returns the PO, or None if it doesn't
    exist. Raises `ValueError` if `PURCHASE_TEAM_EMAIL` isn't configured
    (there's nowhere to send it). Unlike the automatic path this does NOT
    require `ENABLE_PO_EMAIL` -- clicking Resend is an explicit request."""
    from backend.app.core.config import settings

    po = get_purchase_order(po_id, session)
    if po is None:
        return None
    if not settings.purchase_team_email:
        raise ValueError(
            "PURCHASE_TEAM_EMAIL is not configured -- set it before resending PO emails."
        )

    _email_purchase_order(po, session)
    session.flush()
    return po
