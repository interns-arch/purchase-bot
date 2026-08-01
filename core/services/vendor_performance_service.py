"""Aggregates the gap-analysis result (`gap_analysis_service`) into
vendor-level and part-level performance metrics.

Vendor Accuracy % = Total Delivered Quantity / Total Ordered Quantity * 100.
"Number of Purchase Orders" / "Fully Delivered Orders" / "Partial
Deliveries" are counted at the PO (order) level: a PO counts as fully
delivered only if every one of its line items is FULLY DELIVERED.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from core.services.gap_analysis_service import FULLY_DELIVERED, GapRow, compute_gap_analysis

TWOPLACES = Decimal("0.01")


@dataclass
class VendorSummary:
    vendor_name: str
    ordered_qty: Decimal
    delivered_qty: Decimal
    pending_qty: Decimal
    accuracy_pct: Decimal
    po_count: int
    fully_delivered_po_count: int
    partial_po_count: int
    average_shortage: Decimal


@dataclass
class PartPerformance:
    vendor_name: str
    part_number: str
    ordered_qty: Decimal
    delivered_qty: Decimal
    accuracy_pct: Decimal


@dataclass
class PendingItem:
    vendor_name: str
    part_number: str
    pending_qty: Decimal


def _accuracy(delivered: Decimal, ordered: Decimal) -> Decimal:
    if ordered <= 0:
        return Decimal("0")
    return ((delivered / ordered) * 100).quantize(TWOPLACES)


def compute_vendor_summaries(gap_rows: list[GapRow]) -> list[VendorSummary]:
    by_vendor: dict[str, list[GapRow]] = defaultdict(list)
    for row in gap_rows:
        by_vendor[row.vendor_name].append(row)

    summaries: list[VendorSummary] = []
    for vendor_name, rows in by_vendor.items():
        ordered = sum((row.ordered_qty for row in rows), Decimal("0"))
        delivered = sum((row.delivered_qty for row in rows), Decimal("0"))
        pending = sum((row.pending_qty for row in rows), Decimal("0"))

        by_po: dict[str, list[GapRow]] = defaultdict(list)
        for row in rows:
            by_po[row.po_number].append(row)

        fully_delivered_pos = sum(
            1
            for po_rows in by_po.values()
            if all(po_row.status == FULLY_DELIVERED for po_row in po_rows)
        )

        summaries.append(
            VendorSummary(
                vendor_name=vendor_name,
                ordered_qty=ordered,
                delivered_qty=delivered,
                pending_qty=pending,
                accuracy_pct=_accuracy(delivered, ordered),
                po_count=len(by_po),
                fully_delivered_po_count=fully_delivered_pos,
                partial_po_count=len(by_po) - fully_delivered_pos,
                average_shortage=(pending / len(rows)).quantize(TWOPLACES) if rows else Decimal("0"),
            )
        )

    summaries.sort(key=lambda summary: summary.vendor_name)
    return summaries


def compute_part_performance(gap_rows: list[GapRow]) -> list[PartPerformance]:
    by_vendor_part: dict[tuple[str, str], list[GapRow]] = defaultdict(list)
    for row in gap_rows:
        by_vendor_part[(row.vendor_name, row.part_number)].append(row)

    performance: list[PartPerformance] = []
    for (vendor_name, part_number), rows in by_vendor_part.items():
        ordered = sum((row.ordered_qty for row in rows), Decimal("0"))
        delivered = sum((row.delivered_qty for row in rows), Decimal("0"))
        performance.append(
            PartPerformance(
                vendor_name=vendor_name,
                part_number=part_number,
                ordered_qty=ordered,
                delivered_qty=delivered,
                accuracy_pct=_accuracy(delivered, ordered),
            )
        )

    performance.sort(key=lambda row: (row.vendor_name, row.part_number))
    return performance


def compute_pending_items(gap_rows: list[GapRow]) -> list[PendingItem]:
    pending_items = [
        PendingItem(
            vendor_name=row.vendor_name,
            part_number=row.part_number,
            pending_qty=row.pending_qty,
        )
        for row in gap_rows
        if row.pending_qty > 0
    ]
    pending_items.sort(key=lambda row: row.pending_qty, reverse=True)
    return pending_items


def compute_all(session: Session):
    gap_rows = compute_gap_analysis(session)
    return (
        compute_vendor_summaries(gap_rows),
        compute_part_performance(gap_rows),
        compute_pending_items(gap_rows),
    )
