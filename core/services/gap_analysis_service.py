"""Compares Ordered Quantity (from `PurchaseOrderItem`) against Delivered
Quantity (summed from `DeliveryItem`) for every PO line.

Pending Quantity = Ordered Quantity - Delivered Quantity (floored at 0).

Deliberately NOT stored as a mutable column: it's cheap to compute on demand
from the two source-of-truth tables, and a stored value would risk going
stale as more deliveries arrive. `gap_analysis.py` writes the computed
result to `output/gap_report.xlsx`, which is the durable, shareable record.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import DeliveryItem, Part, PurchaseOrder, PurchaseOrderItem, Vendor

FULLY_DELIVERED = "FULLY DELIVERED"
PARTIALLY_DELIVERED = "PARTIALLY DELIVERED"
NOT_DELIVERED = "NOT DELIVERED"


@dataclass
class GapRow:
    vendor_id: int
    vendor_name: str
    po_id: int
    po_number: str
    part_id: int
    part_number: str
    ordered_qty: Decimal
    delivered_qty: Decimal
    pending_qty: Decimal
    status: str


def _status_for(ordered: Decimal, delivered: Decimal) -> str:
    if delivered <= 0:
        return NOT_DELIVERED
    if delivered >= ordered:
        return FULLY_DELIVERED
    return PARTIALLY_DELIVERED


def compute_gap_analysis(session: Session) -> list[GapRow]:
    delivered_by_po_item: dict[int, Decimal] = dict(
        session.execute(
            select(DeliveryItem.po_item_id, func.sum(DeliveryItem.quantity_delivered)).group_by(
                DeliveryItem.po_item_id
            )
        ).all()
    )

    items = session.execute(
        select(PurchaseOrderItem, PurchaseOrder, Vendor, Part)
        .join(PurchaseOrder, PurchaseOrderItem.po_id == PurchaseOrder.id)
        .join(Vendor, PurchaseOrder.vendor_id == Vendor.id)
        .join(Part, PurchaseOrderItem.part_id == Part.id)
        .order_by(Vendor.name, PurchaseOrder.po_number, Part.canonical_part_number)
    ).all()

    gap_rows: list[GapRow] = []
    for po_item, purchase_order, vendor, part in items:
        delivered = delivered_by_po_item.get(po_item.id, Decimal("0"))
        ordered = po_item.quantity_ordered
        pending = max(ordered - delivered, Decimal("0"))

        gap_rows.append(
            GapRow(
                vendor_id=vendor.id,
                vendor_name=vendor.name,
                po_id=purchase_order.id,
                po_number=purchase_order.po_number,
                part_id=part.id,
                part_number=part.canonical_part_number,
                ordered_qty=ordered,
                delivered_qty=delivered,
                pending_qty=pending,
                status=_status_for(ordered, delivered),
            )
        )

    return gap_rows
