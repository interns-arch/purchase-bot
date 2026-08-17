"""Founder Procurement Command Centre -- Phase 1 (spec §1-§4, §8).

One screen answering "what happened today, what's short, what needs me":

  GET /api/command-centre/summary     KPI strip (live DB aggregates)
  GET /api/command-centre/alerts      Red Alert / Action Required centre
  GET /api/command-centre/stock-gaps  live stock vs reservation vs demand (§8)

Technical principles honoured (spec §26): the DATABASE is the only source
(never the Sheet/workbook outputs); every figure is an aggregated query --
full datasets are never shipped to the browser; each KPI/alert carries a
`link` to the page holding the underlying records (spec §23 drill-down);
reservation arithmetic reuses the same live-remaining semantics as the
allocation engine; all day boundaries are business-timezone IST.

Phase 1 deliberately shows QUANTITIES and COUNTS, not money: purchase-price
data is absent from most vendor files today, and spec §28 forbids showing a
number that cannot be traced to an authoritative calculation. Financial KPIs
arrive with the finance phase."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.documents.models import IncomingDocument
from backend.app.integrations.whatsapp import registry
from backend.app.integrations.whatsapp.daily_stock import (
    _ist_today_start_utc,
    vendors_submitted_today,
)
from core.ingestion.column_detector import normalise_part_number
from core.models import (
    Customer,
    CustomerOrder,
    CustomerOrderItem,
    InventoryImport,
    Vendor,
    VendorInventory,
    VendorInvoiceImport,
    VendorInvoiceLineResult,
    VendorPurchaseOrder,
    VendorPurchaseOrderItem,
    VendorSelection,
)
from core.services import delivery_tracking_service
from core.services.vendor_selection_service import _matchable_part_numbers
from core.time_utils import now_ist

router = APIRouter(
    prefix="/api/command-centre",
    tags=["command-centre"],
    dependencies=[Depends(get_current_user)],
)

_RECENT_DAYS = 7  # window for "still actionable" orders/invoices


def _to_naive_utc(ist_moment) -> datetime:
    from datetime import timezone

    return ist_moment.astimezone(timezone.utc).replace(tzinfo=None)


# --- response models --------------------------------------------------------


class VendorsKpi(BaseModel):
    expected: int
    received: int
    pending: int
    pending_names: list[str]
    unregistered_received_today: int


class FilesKpi(BaseModel):
    received: int
    processed: int
    with_errors: int
    failed: int
    needs_review: int
    duplicates: int


class OrdersKpi(BaseModel):
    orders_today: int
    lines_today: int
    qty_ordered_today: float
    qty_allocated_today: float
    qty_short_today: float
    fill_rate_pct: float | None
    at_risk_orders: int  # recent-window orders still short


class StockKpi(BaseModel):
    vendors_with_stock: int
    distinct_parts: int
    total_quantity: float
    reserved_quantity: float
    live_remaining: float


class PoKpi(BaseModel):
    created_today: int
    created_mtd: int
    ordered_qty_today: float
    email_failed: int


class DeliveryKpi(BaseModel):
    ordered_qty: float
    delivered_qty: float
    short_qty: float
    fulfilment_pct: float | None
    not_delivered_lines: int
    partial_lines: int


class InvoiceKpi(BaseModel):
    verified_recent: int
    matched_lines: int
    short_supply: int
    extra_supply: int
    missing_part: int
    unexpected_part: int
    needs_review: int


class CommandCentreSummary(BaseModel):
    generated_at_ist: str
    vendors: VendorsKpi
    files_today: FilesKpi
    orders: OrdersKpi
    stock: StockKpi
    purchase_orders: PoKpi
    delivery: DeliveryKpi
    invoices: InvoiceKpi


class Alert(BaseModel):
    type: str
    severity: str  # "error" | "warning" | "info"
    title: str
    detail: str
    link: str  # frontend route holding the underlying records
    age_hours: float | None = None


class StockGapRow(BaseModel):
    part_number: str
    vendor_stock: float
    reserved: float
    live_remaining: float
    demand: float
    allocated: float
    short: float
    gap: float  # live_remaining - short (negative = cannot be fulfilled)
    vendors: list[str]


# --- shared aggregate helpers ----------------------------------------------


def _order_allocation_totals(order_ids: list[int], db: Session) -> tuple[Decimal, Decimal]:
    """(requested, allocated) across the given orders, one aggregated query
    each -- allocation summed per line then capped by nothing (allocations
    can never exceed requested; the ledger enforces it)."""
    if not order_ids:
        return Decimal(0), Decimal(0)
    requested = db.execute(
        select(func.coalesce(func.sum(CustomerOrderItem.quantity_requested), 0)).where(
            CustomerOrderItem.customer_order_id.in_(order_ids)
        )
    ).scalar_one()
    allocated = db.execute(
        select(func.coalesce(func.sum(VendorSelection.quantity_selected), 0))
        .join(
            CustomerOrderItem,
            VendorSelection.customer_order_item_id == CustomerOrderItem.id,
        )
        .where(CustomerOrderItem.customer_order_id.in_(order_ids))
    ).scalar_one()
    return Decimal(requested), Decimal(allocated)


def _short_orders(db: Session, since: datetime) -> list[dict]:
    """Orders since `since` whose allocated < requested, with per-order
    figures -- the data behind both the at-risk KPI and shortage alerts."""
    allocated_sub = (
        select(
            CustomerOrderItem.customer_order_id.label("order_id"),
            func.sum(CustomerOrderItem.quantity_requested).label("requested"),
        )
        .where(CustomerOrderItem.customer_order_id.isnot(None))
        .group_by(CustomerOrderItem.customer_order_id)
        .subquery()
    )
    orders = db.execute(
        select(CustomerOrder, Customer)
        .outerjoin(Customer, CustomerOrder.customer_id == Customer.id)
        .where(CustomerOrder.created_at >= since)
        .order_by(CustomerOrder.id)
    ).all()
    results = []
    for order, customer in orders:
        requested, allocated = _order_allocation_totals([order.id], db)
        short = requested - allocated
        if short <= 0:
            continue
        results.append(
            {
                "order": order,
                "customer_name": customer.name if customer else None,
                "requested": requested,
                "allocated": allocated,
                "short": short,
            }
        )
    return results


# --- endpoints --------------------------------------------------------------


@router.get("/summary", response_model=CommandCentreSummary)
def command_centre_summary(db: Session = Depends(get_db)) -> CommandCentreSummary:
    today_utc = _ist_today_start_utc()
    ist_now = now_ist()
    mtd_utc = _to_naive_utc(ist_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    recent_utc = today_utc - timedelta(days=_RECENT_DAYS)

    # Vendors expected today = every registered vendor (spec §3 row 1).
    registered = registry.registered_vendors(db)
    submitted_ids = vendors_submitted_today(db)
    registered_ids = {vendor_id for vendor_id, _ in registered}
    pending_names = sorted(
        (name for vendor_id, name in registered if vendor_id not in submitted_ids),
        key=str.lower,
    )
    vendors_kpi = VendorsKpi(
        expected=len(registered),
        received=len(registered_ids & submitted_ids),
        pending=len(pending_names),
        pending_names=pending_names,
        unregistered_received_today=len(submitted_ids - registered_ids),
    )

    # Files today, by document status (one grouped query).
    status_counts = {
        status.value: count
        for status, count in db.execute(
            select(IncomingDocument.status, func.count())
            .where(IncomingDocument.received_at >= today_utc)
            .group_by(IncomingDocument.status)
        )
    }
    files_kpi = FilesKpi(
        received=sum(status_counts.values()),
        processed=status_counts.get("PROCESSED", 0),
        with_errors=status_counts.get("PROCESSED_WITH_ERRORS", 0),
        failed=status_counts.get("FAILED", 0) + status_counts.get("DOWNLOAD_FAILED", 0),
        needs_review=status_counts.get("NEEDS_REVIEW", 0),
        duplicates=status_counts.get("SKIPPED_DUPLICATE", 0),
    )

    # Orders today + at-risk (recent window, still short).
    today_order_ids = list(
        db.execute(
            select(CustomerOrder.id).where(CustomerOrder.created_at >= today_utc)
        ).scalars()
    )
    lines_today = (
        db.execute(
            select(func.count()).select_from(CustomerOrderItem).where(
                CustomerOrderItem.customer_order_id.in_(today_order_ids)
            )
        ).scalar_one()
        if today_order_ids
        else 0
    )
    requested, allocated = _order_allocation_totals(today_order_ids, db)
    short = requested - allocated
    orders_kpi = OrdersKpi(
        orders_today=len(today_order_ids),
        lines_today=lines_today,
        qty_ordered_today=float(requested),
        qty_allocated_today=float(allocated),
        qty_short_today=float(short),
        fill_rate_pct=round(float(allocated / requested * 100), 1) if requested else None,
        at_risk_orders=len(_short_orders(db, recent_utc)),
    )

    # Active stock + live reservations (live remaining = imported - reserved,
    # the same rule the allocation engine enforces).
    stock_row = db.execute(
        select(
            func.count(func.distinct(VendorInventory.vendor_id)),
            func.count(func.distinct(VendorInventory.part_id)),
            func.coalesce(func.sum(VendorInventory.quantity_available), 0),
        )
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .where(InventoryImport.is_active.is_(True))
    ).one()
    reserved_total = db.execute(
        select(func.coalesce(func.sum(VendorSelection.quantity_selected), 0))
        .join(
            VendorInventory,
            (VendorInventory.vendor_id == VendorSelection.vendor_id)
            & (VendorInventory.part_id == VendorSelection.part_id),
        )
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .where(InventoryImport.is_active.is_(True))
    ).scalar_one()
    stock_kpi = StockKpi(
        vendors_with_stock=stock_row[0],
        distinct_parts=stock_row[1],
        total_quantity=float(stock_row[2]),
        reserved_quantity=float(reserved_total),
        live_remaining=float(Decimal(stock_row[2]) - Decimal(reserved_total)),
    )

    # Purchase orders (counts + ordered qty; money arrives with the finance
    # phase -- see module docstring).
    pos_today = db.execute(
        select(func.count()).select_from(VendorPurchaseOrder).where(
            VendorPurchaseOrder.created_at >= today_utc
        )
    ).scalar_one()
    pos_mtd = db.execute(
        select(func.count()).select_from(VendorPurchaseOrder).where(
            VendorPurchaseOrder.created_at >= mtd_utc
        )
    ).scalar_one()
    po_qty_today = db.execute(
        select(func.coalesce(func.sum(VendorPurchaseOrderItem.quantity), 0))
        .join(
            VendorPurchaseOrder,
            VendorPurchaseOrderItem.purchase_order_id == VendorPurchaseOrder.id,
        )
        .where(VendorPurchaseOrder.created_at >= today_utc)
    ).scalar_one()
    po_email_failed = db.execute(
        select(func.count()).select_from(VendorPurchaseOrder).where(
            VendorPurchaseOrder.status == "EMAIL_FAILED"
        )
    ).scalar_one()
    po_kpi = PoKpi(
        created_today=pos_today,
        created_mtd=pos_mtd,
        ordered_qty_today=float(po_qty_today),
        email_failed=po_email_failed,
    )

    # Delivery outstanding -- reuse the authoritative Delivery Tracking
    # computation (spec §12: same source, never a parallel calculation).
    tracking_rows = delivery_tracking_service.compute_rows(db)
    tracking = delivery_tracking_service.compute_summary(tracking_rows)
    total_ordered = float(tracking.total_ordered_qty)
    delivery_kpi = DeliveryKpi(
        ordered_qty=total_ordered,
        delivered_qty=float(tracking.total_delivered_qty),
        short_qty=float(tracking.total_short_qty),
        fulfilment_pct=(
            round(float(tracking.total_delivered_qty) / total_ordered * 100, 1)
            if total_ordered
            else None
        ),
        not_delivered_lines=tracking.not_delivered_count,
        partial_lines=tracking.partial_count,
    )

    # Invoice discrepancies (recent window).
    discrepancy_counts = {
        kind.value: count
        for kind, count in db.execute(
            select(VendorInvoiceLineResult.discrepancy_type, func.count())
            .join(
                VendorInvoiceImport,
                VendorInvoiceLineResult.invoice_import_id == VendorInvoiceImport.id,
            )
            .where(VendorInvoiceImport.created_at >= recent_utc)
            .group_by(VendorInvoiceLineResult.discrepancy_type)
        )
    }
    invoices_recent = db.execute(
        select(func.count()).select_from(VendorInvoiceImport).where(
            VendorInvoiceImport.created_at >= recent_utc
        )
    ).scalar_one()
    needs_review = db.execute(
        select(func.count()).select_from(VendorInvoiceImport).where(
            VendorInvoiceImport.created_at >= recent_utc,
            VendorInvoiceImport.status == "NEEDS_REVIEW",
        )
    ).scalar_one()
    invoice_kpi = InvoiceKpi(
        verified_recent=invoices_recent,
        matched_lines=discrepancy_counts.get("MATCHED", 0),
        short_supply=discrepancy_counts.get("SHORT_SUPPLY", 0),
        extra_supply=discrepancy_counts.get("EXTRA_SUPPLY", 0),
        missing_part=discrepancy_counts.get("MISSING_PART", 0),
        unexpected_part=discrepancy_counts.get("UNEXPECTED_PART", 0),
        needs_review=needs_review,
    )

    return CommandCentreSummary(
        generated_at_ist=ist_now.strftime("%Y-%m-%d %H:%M IST"),
        vendors=vendors_kpi,
        files_today=files_kpi,
        orders=orders_kpi,
        stock=stock_kpi,
        purchase_orders=po_kpi,
        delivery=delivery_kpi,
        invoices=invoice_kpi,
    )


@router.get("/alerts", response_model=list[Alert])
def command_centre_alerts(db: Session = Depends(get_db)) -> list[Alert]:
    """The Red Alert / Action Required centre (spec §4): everything that
    needs a human, most severe first, each linking to its records."""
    alerts: list[Alert] = []
    today_utc = _ist_today_start_utc()
    recent_utc = today_utc - timedelta(days=_RECENT_DAYS)
    now_utc = datetime.utcnow()

    # 1. Import failures today (source, sender, exact reason).
    failures = db.execute(
        select(IncomingDocument)
        .where(
            IncomingDocument.received_at >= today_utc,
            IncomingDocument.status.in_(["FAILED", "DOWNLOAD_FAILED", "NEEDS_REVIEW"]),
        )
        .order_by(IncomingDocument.id.desc())
        .limit(20)
    ).scalars().all()
    for document in failures:
        age = (now_utc - document.received_at).total_seconds() / 3600 if document.received_at else None
        alerts.append(
            Alert(
                type="import_failure",
                severity="error" if document.status.value != "NEEDS_REVIEW" else "warning",
                title=f"{document.document_type.value.replace('_', ' ').title()} "
                f"{'failed' if document.status.value != 'NEEDS_REVIEW' else 'needs review'}: "
                f"{document.filename}",
                detail=(
                    f"Source: {document.source.value}"
                    + (f" | Sender: {document.sender}" if document.sender else "")
                    + (f" | {document.error_message}" if document.error_message else "")
                ),
                link="/file-inbox",
                age_hours=round(age, 1) if age is not None else None,
            )
        )

    # 2. Order shortages (recent window, still short).
    for entry in _short_orders(db, recent_utc):
        order = entry["order"]
        age = (now_utc - order.created_at).total_seconds() / 3600
        who = entry["customer_name"] or order.file_name
        alerts.append(
            Alert(
                type="order_shortage",
                severity="error",
                title=f"Order {order.id} — {who}: short {float(entry['short']):g}",
                detail=(
                    f"Requested {float(entry['requested']):g}, "
                    f"allocated {float(entry['allocated']):g}. "
                    "No vendor currently covers the rest."
                ),
                link="/customer-orders",
                age_hours=round(age, 1),
            )
        )

    # 3. Vendors still pending today.
    registered = registry.registered_vendors(db)
    submitted = vendors_submitted_today(db)
    pending = [(vid, name) for vid, name in registered if vid not in submitted]
    if pending:
        last_seen: dict[int, datetime] = dict(
            db.execute(
                select(InventoryImport.vendor_id, func.max(InventoryImport.created_at))
                .where(InventoryImport.vendor_id.in_([vid for vid, _ in pending]))
                .group_by(InventoryImport.vendor_id)
            ).all()
        )
        names = []
        for vendor_id, name in sorted(pending, key=lambda p: p[1].lower()):
            seen = last_seen.get(vendor_id)
            names.append(f"{name} (last: {seen.strftime('%d %b') if seen else 'never'})")
        alerts.append(
            Alert(
                type="vendor_stock_pending",
                severity="warning",
                title=f"{len(pending)} vendor(s) have not sent today's stock",
                detail="; ".join(names),
                link="/vendor-inventory",
            )
        )

    # 4. Invoice discrepancies (recent, non-matched lines grouped by invoice).
    rows = db.execute(
        select(
            VendorInvoiceImport,
            Vendor.name,
            func.count(VendorInvoiceLineResult.id),
        )
        .join(
            VendorInvoiceLineResult,
            VendorInvoiceLineResult.invoice_import_id == VendorInvoiceImport.id,
        )
        .outerjoin(Vendor, VendorInvoiceImport.vendor_id == Vendor.id)
        .where(
            VendorInvoiceImport.created_at >= recent_utc,
            VendorInvoiceLineResult.discrepancy_type != "MATCHED",
        )
        .group_by(VendorInvoiceImport.id, Vendor.name)
        .order_by(VendorInvoiceImport.id.desc())
        .limit(10)
    ).all()
    for invoice, vendor_name, mismatch_count in rows:
        alerts.append(
            Alert(
                type="invoice_discrepancy",
                severity="warning",
                title=(
                    f"Invoice {invoice.file_name} — "
                    f"{vendor_name or invoice.vendor_name_extracted or 'unknown vendor'}: "
                    f"{mismatch_count} mismatched line(s)"
                ),
                detail="Short/extra/missing/unexpected lines need review.",
                link="/vendor-invoices",
            )
        )

    # 5. PO emails that failed to reach the purchase team.
    failed_pos = db.execute(
        select(VendorPurchaseOrder, Vendor.name)
        .join(Vendor, VendorPurchaseOrder.vendor_id == Vendor.id)
        .where(VendorPurchaseOrder.status == "EMAIL_FAILED")
        .order_by(VendorPurchaseOrder.id.desc())
        .limit(10)
    ).all()
    for po, vendor_name in failed_pos:
        alerts.append(
            Alert(
                type="po_email_failed",
                severity="warning",
                title=f"PO {po.po_number} ({vendor_name}) — internal email failed",
                detail="Resend from the Purchase Orders page.",
                link="/purchase-orders",
            )
        )

    severity_rank = {"error": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda alert: severity_rank.get(alert.severity, 3))
    return alerts


@router.get("/stock-gaps", response_model=list[StockGapRow])
def command_centre_stock_gaps(db: Session = Depends(get_db)) -> list[StockGapRow]:
    """Spec §8: per part -- imported stock, live reservations, live
    remaining, open customer demand, and the gap. Restricted to parts on
    recent SHORT order lines (the actionable set), worst gap first."""
    today_utc = _ist_today_start_utc()
    recent_utc = today_utc - timedelta(days=_RECENT_DAYS)

    items = db.execute(
        select(CustomerOrderItem)
        .join(CustomerOrder, CustomerOrderItem.customer_order_id == CustomerOrder.id)
        .where(CustomerOrder.created_at >= recent_utc)
    ).scalars().all()

    by_part: dict[str, dict] = {}
    for item in items:
        allocated = db.execute(
            select(func.coalesce(func.sum(VendorSelection.quantity_selected), 0)).where(
                VendorSelection.customer_order_item_id == item.id
            )
        ).scalar_one()
        short = Decimal(item.quantity_requested) - Decimal(allocated)
        normalized = normalise_part_number(item.part_number_raw)
        entry = by_part.setdefault(
            normalized,
            {"raw": item.part_number_raw, "demand": Decimal(0), "allocated": Decimal(0),
             "short": Decimal(0)},
        )
        entry["demand"] += Decimal(item.quantity_requested)
        entry["allocated"] += Decimal(allocated)
        entry["short"] += max(short, Decimal(0))

    rows: list[StockGapRow] = []
    for normalized, entry in by_part.items():
        if entry["short"] <= 0:
            continue
        numbers = _matchable_part_numbers(normalized, db)
        stock_rows = db.execute(
            select(VendorInventory, Vendor.name)
            .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
            .join(Vendor, VendorInventory.vendor_id == Vendor.id)
            .where(
                VendorInventory.normalized_part_number.in_(numbers),
                InventoryImport.is_active.is_(True),
            )
        ).all()
        vendor_stock = sum((Decimal(r.quantity_available) for r, _ in stock_rows), Decimal(0))
        reserved = Decimal(0)
        for inventory_row, _name in stock_rows:
            reserved += db.execute(
                select(func.coalesce(func.sum(VendorSelection.quantity_selected), 0)).where(
                    VendorSelection.vendor_id == inventory_row.vendor_id,
                    VendorSelection.part_id == inventory_row.part_id,
                )
            ).scalar_one()
        live = vendor_stock - reserved
        rows.append(
            StockGapRow(
                part_number=entry["raw"],
                vendor_stock=float(vendor_stock),
                reserved=float(reserved),
                live_remaining=float(live),
                demand=float(entry["demand"]),
                allocated=float(entry["allocated"]),
                short=float(entry["short"]),
                gap=float(live - entry["short"]),
                vendors=sorted({name for _, name in stock_rows}),
            )
        )

    rows.sort(key=lambda row: row.gap)
    return rows[:25]
