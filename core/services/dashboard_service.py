"""Read-only aggregation for the web dashboard. New module (Phase 1 of the
web app has no CLI predecessor to reuse) -- it composes existing tables and
services the same way `order_matching.py`/`vendor_performance.py` already
query the database directly, rather than introducing a new abstraction.

The dashboard is shaped around the actual business workflow (vendors ->
inventory -> customer orders -> vendor comparison), not raw table counts:
"Parts Matched" / "Parts Not Found" reflect the most recent customer order's
comparison run, computed on demand via `vendor_comparison_service` --
nothing about a comparison is persisted, consistent with how Gap Analysis
and Vendor Performance are also always computed fresh from source tables
rather than cached.

Pure business logic -- no FastAPI/print()/input() here, same rule as every
other module under `core/services/`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from core.ingestion.column_detector import normalise_part_number
from core.models import (
    CustomerOrder,
    CustomerOrderItem,
    InventoryImport,
    Part,
    Vendor,
    VendorInventory,
)


@dataclass
class RecentActivityEntry:
    activity_type: str  # "INVENTORY_IMPORT" | "CUSTOMER_ORDER"
    reference_id: int
    label: str
    file_name: str
    status: str
    row_count: int
    error_count: int
    created_at: datetime


@dataclass
class DashboardSummary:
    active_vendors: int
    total_vendors: int
    total_inventory_imports: int
    total_customer_orders: int
    parts_matched: int
    parts_not_found: int
    last_import_at: datetime | None = None
    recent_activity: list[RecentActivityEntry] = field(default_factory=list)


def _latest_order_match_counts(session: Session) -> tuple[int, int]:
    """(matched, not_found) part counts for the most recent customer order.

    Deliberately does NOT call `compare_vendors_for_order`: that builds the
    entire master inventory to produce two integers, which measured at ~6,869
    SQL statements and made this endpoint take ~357s in production. The same
    two counters are derived here with two bounded queries -- the order's own
    items, then a single membership check against currently-active vendor
    inventory. Matching semantics are unchanged (same normalisation, same
    "a part is matched when some active vendor stocks it" rule, same handling
    of blank/non-positive rows as invalid and therefore uncounted)."""
    latest_order = session.execute(
        select(CustomerOrder).order_by(CustomerOrder.created_at.desc()).limit(1)
    ).scalar_one_or_none()

    if latest_order is None:
        return 0, 0

    items = session.execute(
        select(CustomerOrderItem.part_number_raw, CustomerOrderItem.quantity_requested)
        .where(CustomerOrderItem.customer_order_id == latest_order.id)
    ).all()

    wanted: list[str] = []
    for raw_part_number, quantity_requested in items:
        normalized = normalise_part_number(raw_part_number)
        if not normalized or quantity_requested is None or quantity_requested <= 0:
            continue  # invalid line -- neither matched nor not-found
        wanted.append(normalized)

    if not wanted:
        return 0, 0

    stocked = set(
        session.execute(
            select(VendorInventory.normalized_part_number)
            .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
            .where(
                InventoryImport.is_active.is_(True),
                VendorInventory.part_id.isnot(None),
                VendorInventory.normalized_part_number.in_(set(wanted)),
            )
            .distinct()
        ).scalars()
    )

    matched = sum(1 for part_number in wanted if part_number in stocked)
    return matched, len(wanted) - matched


def get_dashboard_summary(session: Session, *, recent_limit: int = 10) -> DashboardSummary:
    total_vendors = session.execute(select(func.count()).select_from(Vendor)).scalar_one()
    active_vendors = session.execute(
        select(func.count()).select_from(Vendor).where(Vendor.active.is_(True))
    ).scalar_one()
    total_inventory_imports = session.execute(
        select(func.count()).select_from(InventoryImport)
    ).scalar_one()
    total_customer_orders = session.execute(
        select(func.count()).select_from(CustomerOrder)
    ).scalar_one()

    parts_matched, parts_not_found = _latest_order_match_counts(session)

    inventory_rows = list(
        session.execute(
            select(InventoryImport)
            .options(selectinload(InventoryImport.vendor))
            .order_by(InventoryImport.created_at.desc())
            .limit(recent_limit)
        ).scalars()
    )
    order_rows = list(
        session.execute(
            select(CustomerOrder).order_by(CustomerOrder.created_at.desc()).limit(recent_limit)
        ).scalars()
    )

    activity = [
        RecentActivityEntry(
            activity_type="INVENTORY_IMPORT",
            reference_id=row.id,
            label=row.vendor.name,
            file_name=row.file_name,
            status=row.status.value,
            row_count=row.row_count,
            error_count=row.error_count,
            created_at=row.created_at,
        )
        for row in inventory_rows
    ] + [
        RecentActivityEntry(
            activity_type="CUSTOMER_ORDER",
            reference_id=row.id,
            label="Customer Order",
            file_name=row.file_name,
            status=row.status.value,
            row_count=row.row_count,
            error_count=row.error_count,
            created_at=row.created_at,
        )
        for row in order_rows
    ]
    activity.sort(key=lambda entry: entry.created_at, reverse=True)
    activity = activity[:recent_limit]

    last_import_at = inventory_rows[0].created_at if inventory_rows else None

    return DashboardSummary(
        active_vendors=active_vendors,
        total_vendors=total_vendors,
        total_inventory_imports=total_inventory_imports,
        total_customer_orders=total_customer_orders,
        parts_matched=parts_matched,
        parts_not_found=parts_not_found,
        last_import_at=last_import_at,
        recent_activity=activity,
    )
