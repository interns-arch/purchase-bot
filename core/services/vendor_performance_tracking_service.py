"""Vendor Performance dashboard: built entirely on top of
`delivery_tracking_service.py`'s (vendor, part) reconciliation -- this
module never re-derives ordered/delivered/short quantities itself, only
aggregates that same row set up to the vendor level. This answers "which
vendor promises N pieces but repeatedly delivers less?"

Computed fresh on every call, same convention as `delivery_tracking_service.py`
and `dashboard_service.py`. Deliberately separate from the CLI-shared
`vendor_performance_service.py` (which aggregates `purchase_order_items` +
`delivery_items` for the pre-existing CLI pipeline) -- that module stays
untouched.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import CustomerOrder, CustomerOrderItem, Part, Vendor, VendorDeliveryImport, VendorDeliveryItem, VendorSelection
from core.services import delivery_tracking_service

TWOPLACES = Decimal("0.01")


@dataclass
class VendorPerformanceRow:
    vendor_id: int
    vendor_name: str
    parts_allocated: int
    ordered_qty: Decimal
    delivered_qty: Decimal
    short_qty: Decimal
    fulfillment_pct: Decimal
    accuracy_pct: Decimal


@dataclass
class VendorPerformanceSummary:
    best_vendor_name: str | None
    best_vendor_fulfillment_pct: Decimal | None
    lowest_vendor_name: str | None
    lowest_vendor_fulfillment_pct: Decimal | None
    average_fulfillment_pct: Decimal
    total_vendors: int
    total_deliveries: int
    total_short_qty: Decimal


@dataclass
class MonthlyTrendPoint:
    month: str  # "YYYY-MM"
    delivered_qty: Decimal
    accuracy_pct: Decimal


@dataclass
class VendorDetailSelectionRow:
    customer_order_id: int
    customer_order_file_name: str
    part_number: str
    quantity_selected: Decimal
    selected_at: datetime


@dataclass
class VendorDetailDeliveryRow:
    part_number: str
    quantity_delivered: Decimal
    delivery_date: date | None
    file_name: str


@dataclass
class VendorDetail:
    vendor_id: int
    vendor_name: str
    performance: VendorPerformanceRow | None
    delivery_rows: list[delivery_tracking_service.DeliveryTrackingRow] = field(default_factory=list)
    selections: list[VendorDetailSelectionRow] = field(default_factory=list)
    deliveries: list[VendorDetailDeliveryRow] = field(default_factory=list)


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return (numerator / denominator * 100).quantize(TWOPLACES)


def compute_vendor_rows(
    session: Session,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    vendor_id: int | None = None,
    customer_order_id: int | None = None,
    part_number: str | None = None,
) -> list[VendorPerformanceRow]:
    delivery_rows = delivery_tracking_service.compute_rows(
        session,
        date_from=date_from,
        date_to=date_to,
        vendor_id=vendor_id,
        customer_order_id=customer_order_id,
        part_number=part_number,
    )

    by_vendor: dict[int, list[delivery_tracking_service.DeliveryTrackingRow]] = defaultdict(list)
    for row in delivery_rows:
        by_vendor[row.vendor_id].append(row)

    result: list[VendorPerformanceRow] = []
    for vid, rows in by_vendor.items():
        ordered = sum((row.ordered_qty for row in rows), Decimal("0"))
        delivered = sum((row.delivered_qty for row in rows), Decimal("0"))
        short = sum((row.short_qty for row in rows), Decimal("0"))
        complete_count = sum(1 for row in rows if row.status == delivery_tracking_service.COMPLETE)

        result.append(
            VendorPerformanceRow(
                vendor_id=vid,
                vendor_name=rows[0].vendor_name,
                parts_allocated=len(rows),
                ordered_qty=ordered,
                delivered_qty=delivered,
                short_qty=short,
                fulfillment_pct=_pct(delivered, ordered),
                accuracy_pct=_pct(Decimal(complete_count), Decimal(len(rows))),
            )
        )

    result.sort(key=lambda row: row.fulfillment_pct, reverse=True)
    return result


def compute_summary(
    rows: list[VendorPerformanceRow], session: Session
) -> VendorPerformanceSummary:
    if not rows:
        return VendorPerformanceSummary(
            best_vendor_name=None,
            best_vendor_fulfillment_pct=None,
            lowest_vendor_name=None,
            lowest_vendor_fulfillment_pct=None,
            average_fulfillment_pct=Decimal("0"),
            total_vendors=0,
            total_deliveries=0,
            total_short_qty=Decimal("0"),
        )

    # `rows` is already sorted by fulfillment_pct desc (see compute_vendor_rows).
    best = rows[0]
    lowest = rows[-1]
    average = (sum((row.fulfillment_pct for row in rows), Decimal("0")) / len(rows)).quantize(
        TWOPLACES
    )
    total_deliveries = session.execute(
        select(func.count()).select_from(VendorDeliveryItem)
    ).scalar_one()
    total_short = sum((row.short_qty for row in rows), Decimal("0"))

    return VendorPerformanceSummary(
        best_vendor_name=best.vendor_name,
        best_vendor_fulfillment_pct=best.fulfillment_pct,
        lowest_vendor_name=lowest.vendor_name,
        lowest_vendor_fulfillment_pct=lowest.fulfillment_pct,
        average_fulfillment_pct=average,
        total_vendors=len(rows),
        total_deliveries=total_deliveries,
        total_short_qty=total_short,
    )


def compute_monthly_trend(session: Session, *, vendor_id: int | None = None) -> list[MonthlyTrendPoint]:
    """Delivered quantity per month (volume trend), plus that month's
    accuracy -- of every (vendor, part) pair that received a delivery in
    that month, what fraction are (as of now) fully complete against their
    overall ordered quantity."""
    delivery_rows = delivery_tracking_service.compute_rows(session, vendor_id=vendor_id)
    status_by_pair = {(row.vendor_id, row.part_id): row.status for row in delivery_rows}

    monthly_delivered: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    monthly_pairs: dict[str, set[tuple[int, int]]] = defaultdict(set)

    for item, effective_date in delivery_tracking_service.fetch_delivery_items_with_dates(
        session, vendor_id=vendor_id
    ):
        month_key = effective_date.strftime("%Y-%m")
        monthly_delivered[month_key] += item.quantity_delivered
        monthly_pairs[month_key].add((item.vendor_id, item.part_id))

    points: list[MonthlyTrendPoint] = []
    for month_key in sorted(monthly_delivered):
        pairs = monthly_pairs[month_key]
        complete_count = sum(
            1 for pair in pairs if status_by_pair.get(pair) == delivery_tracking_service.COMPLETE
        )
        points.append(
            MonthlyTrendPoint(
                month=month_key,
                delivered_qty=monthly_delivered[month_key],
                accuracy_pct=_pct(Decimal(complete_count), Decimal(len(pairs))) if pairs else Decimal("0"),
            )
        )

    return points


def compute_vendor_detail(vendor_id: int, session: Session) -> VendorDetail:
    """Raises `LookupError` if the vendor doesn't exist. Everything a
    purchase manager needs to answer "why is this vendor's number what it
    is": every order line allocated to them, every delivery recorded
    against them, and the same reconciled (vendor, part) rows the main
    dashboard shows, scoped to just this vendor."""
    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        raise LookupError(f"Vendor {vendor_id} not found.")

    selection_query = (
        select(VendorSelection, CustomerOrder, Part)
        .join(CustomerOrderItem, VendorSelection.customer_order_item_id == CustomerOrderItem.id)
        .join(CustomerOrder, CustomerOrderItem.customer_order_id == CustomerOrder.id)
        .join(Part, VendorSelection.part_id == Part.id)
        .where(VendorSelection.vendor_id == vendor_id)
        .order_by(CustomerOrder.created_at.desc())
    )
    selections = [
        VendorDetailSelectionRow(
            customer_order_id=order.id,
            customer_order_file_name=order.file_name,
            part_number=part.canonical_part_number,
            quantity_selected=selection.quantity_selected,
            selected_at=selection.created_at,
        )
        for selection, order, part in session.execute(selection_query).all()
    ]

    delivery_query = (
        select(VendorDeliveryItem, Part, VendorDeliveryImport)
        .join(Part, VendorDeliveryItem.part_id == Part.id)
        .join(
            VendorDeliveryImport,
            VendorDeliveryItem.vendor_delivery_import_id == VendorDeliveryImport.id,
        )
        .where(VendorDeliveryItem.vendor_id == vendor_id)
        .order_by(VendorDeliveryImport.created_at.desc())
    )
    deliveries = [
        VendorDetailDeliveryRow(
            part_number=part.canonical_part_number,
            quantity_delivered=item.quantity_delivered,
            delivery_date=item.delivery_date,
            file_name=vendor_delivery_import.file_name,
        )
        for item, part, vendor_delivery_import in session.execute(delivery_query).all()
    ]

    delivery_rows = delivery_tracking_service.compute_rows(session, vendor_id=vendor_id)
    performance_rows = compute_vendor_rows(session, vendor_id=vendor_id)

    return VendorDetail(
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        performance=performance_rows[0] if performance_rows else None,
        delivery_rows=delivery_rows,
        selections=selections,
        deliveries=deliveries,
    )
