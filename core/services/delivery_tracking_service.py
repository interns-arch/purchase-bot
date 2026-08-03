"""Delivery Tracking dashboard: reconciles `VendorSelection` allocations
(the "ordered" side, made on the Vendor Comparison page) against
`VendorDeliveryItem` rows (the "delivered" side, uploaded here) -- both
matched purely by **vendor + part**, since this application has no
Purchase Order concept. Computed fresh on every call (same "never
cache/persist aggregation" convention as `dashboard_service.py`).

Aggregation granularity is (vendor, part), not per-order-line: a vendor
delivery file lists parts shipped, not customer order IDs, so there is no
source data to split a vendor's delivered quantity back across the
multiple orders it might be fulfilling. The `customer_order_id` filter
therefore narrows *which* (vendor, part) pairs appear (only those with at
least one selection belonging to that order) rather than attempting to
re-attribute quantities per order.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import CustomerOrderItem, Part, Vendor, VendorDeliveryItem, VendorSelection

COMPLETE = "COMPLETE"
PARTIAL = "PARTIAL"
NOT_DELIVERED = "NOT_DELIVERED"


@dataclass
class DeliveryTrackingRow:
    vendor_id: int
    vendor_name: str
    part_id: int
    part_number: str
    ordered_qty: Decimal
    delivered_qty: Decimal
    short_qty: Decimal
    status: str
    last_delivery_date: date | None


@dataclass
class DeliveryTrackingSummary:
    total_ordered_qty: Decimal
    total_delivered_qty: Decimal
    total_short_qty: Decimal
    complete_count: int
    partial_count: int
    not_delivered_count: int


@dataclass
class DailyDeliveryPoint:
    delivery_date: date
    delivered_qty: Decimal


@dataclass
class VendorDeliveryPoint:
    vendor_name: str
    delivered_qty: Decimal


def _status_for(ordered: Decimal, delivered: Decimal) -> str:
    if delivered <= 0:
        return NOT_DELIVERED
    if delivered >= ordered:
        return COMPLETE
    return PARTIAL


def _effective_date(item: VendorDeliveryItem) -> date:
    return item.delivery_date or item.vendor_delivery_import.created_at.date()


def fetch_delivery_items_with_dates(
    session: Session,
    *,
    vendor_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[tuple[VendorDeliveryItem, date]]:
    query = select(VendorDeliveryItem)
    if vendor_id is not None:
        query = query.where(VendorDeliveryItem.vendor_id == vendor_id)

    results: list[tuple[VendorDeliveryItem, date]] = []
    for item in session.execute(query).scalars():
        effective_date = _effective_date(item)
        if date_from is not None and effective_date < date_from:
            continue
        if date_to is not None and effective_date > date_to:
            continue
        results.append((item, effective_date))
    return results


def compute_rows(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    vendor_id: int | None = None,
    customer_order_id: int | None = None,
    part_number: str | None = None,
    status: str | None = None,
) -> list[DeliveryTrackingRow]:
    selection_query = (
        select(VendorSelection, Vendor, Part)
        .join(CustomerOrderItem, VendorSelection.customer_order_item_id == CustomerOrderItem.id)
        .join(Vendor, VendorSelection.vendor_id == Vendor.id)
        .join(Part, VendorSelection.part_id == Part.id)
    )
    if vendor_id is not None:
        selection_query = selection_query.where(VendorSelection.vendor_id == vendor_id)
    if customer_order_id is not None:
        selection_query = selection_query.where(
            CustomerOrderItem.customer_order_id == customer_order_id
        )

    ordered_by_pair: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    vendor_names: dict[int, str] = {}
    part_numbers: dict[int, str] = {}

    for selection, vendor, part in session.execute(selection_query).all():
        key = (vendor.id, part.id)
        ordered_by_pair[key] += selection.quantity_selected
        vendor_names[vendor.id] = vendor.name
        part_numbers[part.id] = part.canonical_part_number

    delivered_by_pair: dict[tuple[int, int], Decimal] = defaultdict(lambda: Decimal("0"))
    last_delivery_by_pair: dict[tuple[int, int], date] = {}

    for item, effective_date in fetch_delivery_items_with_dates(
        session, vendor_id=vendor_id, date_from=date_from, date_to=date_to
    ):
        key = (item.vendor_id, item.part_id)
        delivered_by_pair[key] += item.quantity_delivered
        if key not in last_delivery_by_pair or effective_date > last_delivery_by_pair[key]:
            last_delivery_by_pair[key] = effective_date

    # Only pairs with a real allocation are tracked -- a delivery with no
    # matching selection has nothing to reconcile against and would produce
    # a nonsensical "0 ordered" status.
    rows = []
    for (vendor_id_, part_id_), ordered in ordered_by_pair.items():
        pair_key = (vendor_id_, part_id_)
        delivered = delivered_by_pair.get(pair_key, Decimal("0"))
        rows.append(
            DeliveryTrackingRow(
                vendor_id=vendor_id_,
                vendor_name=vendor_names.get(vendor_id_, "-"),
                part_id=part_id_,
                part_number=part_numbers.get(part_id_, "-"),
                ordered_qty=ordered,
                delivered_qty=delivered,
                short_qty=max(ordered - delivered, Decimal("0")),
                status=_status_for(ordered, delivered),
                last_delivery_date=last_delivery_by_pair.get(pair_key),
            )
        )

    if part_number:
        query_lower = part_number.strip().lower()
        rows = [row for row in rows if query_lower in row.part_number.lower()]
    if status:
        rows = [row for row in rows if row.status == status]

    rows.sort(key=lambda row: (row.vendor_name.lower(), row.part_number))
    return rows


def compute_summary(rows: list[DeliveryTrackingRow]) -> DeliveryTrackingSummary:
    return DeliveryTrackingSummary(
        total_ordered_qty=sum((row.ordered_qty for row in rows), Decimal("0")),
        total_delivered_qty=sum((row.delivered_qty for row in rows), Decimal("0")),
        total_short_qty=sum((row.short_qty for row in rows), Decimal("0")),
        complete_count=sum(1 for row in rows if row.status == COMPLETE),
        partial_count=sum(1 for row in rows if row.status == PARTIAL),
        not_delivered_count=sum(1 for row in rows if row.status == NOT_DELIVERED),
    )


def compute_daily_deliveries(
    session: Session,
    *,
    vendor_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[DailyDeliveryPoint]:
    """Not affected by the `part_number`/`status`/`customer_order_id`
    filters -- this is a raw-delivery-event view (day the vendor shipped
    something), not the derived (vendor, part) aggregate the table shows."""
    totals: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for item, effective_date in fetch_delivery_items_with_dates(
        session, vendor_id=vendor_id, date_from=date_from, date_to=date_to
    ):
        totals[effective_date] += item.quantity_delivered

    return [
        DailyDeliveryPoint(delivery_date=day, delivered_qty=qty)
        for day, qty in sorted(totals.items())
    ]


def compute_vendorwise_deliveries(rows: list[DeliveryTrackingRow]) -> list[VendorDeliveryPoint]:
    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for row in rows:
        totals[row.vendor_name] += row.delivered_qty

    points = [
        VendorDeliveryPoint(vendor_name=name, delivered_qty=qty) for name, qty in totals.items()
    ]
    points.sort(key=lambda point: point.delivered_qty, reverse=True)
    return points
