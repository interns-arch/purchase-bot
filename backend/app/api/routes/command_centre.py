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

    # 5. OVERDUE POs (Founder's rule: no vendor invoice uploaded within
    #    PO_INVOICE_DUE_HOURS of PO generation -- the invoice upload is the
    #    signal the goods actually arrived).
    due_hours = _po_invoice_due_hours()
    due_cutoff = now_utc - timedelta(hours=due_hours)
    aged_pos = db.execute(
        select(VendorPurchaseOrder, Vendor.name)
        .join(Vendor, VendorPurchaseOrder.vendor_id == Vendor.id)
        .where(VendorPurchaseOrder.created_at <= due_cutoff)
        .order_by(VendorPurchaseOrder.created_at)
        .limit(30)
    ).all()
    for po, vendor_name in aged_pos:
        if _po_is_invoiced(po, db):
            continue
        age = (now_utc - po.created_at).total_seconds() / 3600
        alerts.append(
            Alert(
                type="po_overdue",
                severity="error",
                title=f"PO {po.po_number} ({vendor_name}) — no invoice after "
                f"{round(age)}h",
                detail=(
                    f"Generated {po.created_at.strftime('%d %b %H:%M')} UTC; the vendor's "
                    f"invoice has not been uploaded (due within {due_hours}h). "
                    "Follow up with the vendor or upload the invoice."
                ),
                link="/purchase-orders",
                age_hours=round(age, 1),
            )
        )

    # 6. PO emails that failed to reach the purchase team.
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


# ===========================================================================
# Phase 2 -- funnel (§5), control towers (§7, §11, §12), vendor scorecard +
# stock-trust (§9, §10), trends (§21). Same principles: aggregated queries,
# quantities/counts only (no un-traceable money), drill links everywhere.
# ===========================================================================


def _window_start(days: int) -> datetime:
    """IST-midnight-aligned start for a `days` window (days=1 -> today)."""
    return _ist_today_start_utc() - timedelta(days=max(days - 1, 0))


class FunnelStage(BaseModel):
    stage: str
    count: int
    quantity: float
    conversion_pct: float | None  # vs previous stage's quantity
    link: str


@router.get("/funnel", response_model=list[FunnelStage])
def command_centre_funnel(days: int = 7, db: Session = Depends(get_db)) -> list[FunnelStage]:
    """Vendor Stock → Orders → Allocation → PO → Invoice → Delivery, with
    quantity at every stage and stage-to-stage conversion (spec §5)."""
    since = _window_start(days)

    stock_count, stock_qty = db.execute(
        select(func.count(func.distinct(InventoryImport.vendor_id)),
               func.coalesce(func.sum(VendorInventory.quantity_available), 0))
        .select_from(VendorInventory)
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .where(InventoryImport.created_at >= since)
    ).one()

    order_ids = list(db.execute(
        select(CustomerOrder.id).where(CustomerOrder.created_at >= since)
    ).scalars())
    ordered, allocated = _order_allocation_totals(order_ids, db)

    po_count, po_qty = db.execute(
        select(func.count(func.distinct(VendorPurchaseOrder.id)),
               func.coalesce(func.sum(VendorPurchaseOrderItem.quantity), 0))
        .select_from(VendorPurchaseOrder)
        .outerjoin(VendorPurchaseOrderItem,
                   VendorPurchaseOrderItem.purchase_order_id == VendorPurchaseOrder.id)
        .where(VendorPurchaseOrder.created_at >= since)
    ).one()

    invoice_count, invoiced_qty = db.execute(
        select(func.count(func.distinct(VendorInvoiceImport.id)),
               func.coalesce(func.sum(VendorInvoiceLineResult.quantity_invoiced), 0))
        .select_from(VendorInvoiceImport)
        .outerjoin(VendorInvoiceLineResult,
                   VendorInvoiceLineResult.invoice_import_id == VendorInvoiceImport.id)
        .where(VendorInvoiceImport.created_at >= since)
    ).one()

    tracking = delivery_tracking_service.compute_summary(
        delivery_tracking_service.compute_rows(db)
    )

    stages_raw = [
        ("Vendor Stock", stock_count, float(stock_qty), "/vendor-inventory"),
        ("Customer Orders", len(order_ids), float(ordered), "/customer-orders"),
        ("Allocation", len(order_ids), float(allocated), "/vendor-comparison"),
        ("Purchase Orders", po_count, float(po_qty), "/purchase-orders"),
        ("Invoices", invoice_count, float(invoiced_qty), "/vendor-invoices"),
        ("Delivered", tracking.complete_count + tracking.partial_count,
         float(tracking.total_delivered_qty), "/delivery-tracking"),
    ]
    stages: list[FunnelStage] = []
    previous_qty: float | None = None
    for name, count, quantity, link in stages_raw:
        conversion = (
            round(quantity / previous_qty * 100, 1)
            if previous_qty and quantity is not None
            else None
        )
        stages.append(FunnelStage(
            stage=name, count=count, quantity=quantity, conversion_pct=conversion, link=link,
        ))
        previous_qty = quantity if quantity else previous_qty
    return stages


class OrderTower(BaseModel):
    total: int
    fully_allocated: int
    partially_allocated: int
    unallocated: int
    awaiting_po: int
    ageing: dict[str, int]  # bucket label -> count of orders STILL short
    customer_fill: list[dict]  # {customer, ordered, allocated, fill_pct}


@router.get("/order-tower", response_model=OrderTower)
def command_centre_order_tower(days: int = 7, db: Session = Depends(get_db)) -> OrderTower:
    """Customer Order Control Tower (spec §7): allocation status buckets,
    ageing of still-short orders, customer-wise fill rate."""
    since = _window_start(days)
    now_utc = datetime.utcnow()
    orders = db.execute(
        select(CustomerOrder, Customer)
        .outerjoin(Customer, CustomerOrder.customer_id == Customer.id)
        .where(CustomerOrder.created_at >= since)
    ).all()

    po_order_ids = set(db.execute(
        select(func.distinct(VendorPurchaseOrder.customer_order_id))
    ).scalars())

    buckets = {"0-1h": 0, "1-3h": 0, "3-6h": 0, "6-24h": 0, "24h+": 0}
    fully = partially = unallocated = awaiting_po = 0
    per_customer: dict[str, dict] = {}
    for order, customer in orders:
        requested, allocated = _order_allocation_totals([order.id], db)
        who = customer.name if customer else order.file_name
        entry = per_customer.setdefault(who, {"ordered": Decimal(0), "allocated": Decimal(0)})
        entry["ordered"] += requested
        entry["allocated"] += allocated

        if requested <= 0:
            continue
        if allocated <= 0:
            unallocated += 1
        elif allocated >= requested:
            fully += 1
            if order.id not in po_order_ids:
                awaiting_po += 1
        else:
            partially += 1
            if order.id not in po_order_ids:
                awaiting_po += 1

        if allocated < requested:
            age_hours = (now_utc - order.created_at).total_seconds() / 3600
            if age_hours <= 1:
                buckets["0-1h"] += 1
            elif age_hours <= 3:
                buckets["1-3h"] += 1
            elif age_hours <= 6:
                buckets["3-6h"] += 1
            elif age_hours <= 24:
                buckets["6-24h"] += 1
            else:
                buckets["24h+"] += 1

    customer_fill = [
        {
            "customer": who,
            "ordered": float(entry["ordered"]),
            "allocated": float(entry["allocated"]),
            "fill_pct": round(float(entry["allocated"] / entry["ordered"] * 100), 1)
            if entry["ordered"]
            else None,
        }
        for who, entry in sorted(per_customer.items(), key=lambda kv: kv[0].lower())
    ]
    return OrderTower(
        total=len(orders),
        fully_allocated=fully,
        partially_allocated=partially,
        unallocated=unallocated,
        awaiting_po=awaiting_po,
        ageing=buckets,
        customer_fill=customer_fill,
    )


def _po_invoice_due_hours() -> int:
    """The Founder's overdue rule: a PO is OVERDUE when its vendor's invoice
    has not been uploaded within this many hours of PO generation (the
    invoice upload is what tells the system the goods actually arrived)."""
    import os

    try:
        return max(1, int(os.environ.get("PO_INVOICE_DUE_HOURS", "24")))
    except ValueError:
        return 24


def _po_is_invoiced(po, db: Session) -> bool:
    """An invoice from this PO's vendor uploaded AFTER the PO was created."""
    count = db.execute(
        select(func.count()).select_from(VendorInvoiceImport).where(
            VendorInvoiceImport.vendor_id == po.vendor_id,
            VendorInvoiceImport.created_at >= po.created_at,
        )
    ).scalar_one()
    return count > 0


class PoTower(BaseModel):
    created_in_window: int
    generated: int
    emailed: int
    email_failed: int
    complete: int
    partial: int
    not_supplied: int
    overdue: int  # older than PO_INVOICE_DUE_HOURS with no vendor invoice
    ageing: dict[str, int]


@router.get("/po-tower", response_model=PoTower)
def command_centre_po_tower(days: int = 30, db: Session = Depends(get_db)) -> PoTower:
    """Purchase Order Control Tower (spec §11): status, supply completeness
    (from the same Delivery Tracking source, spec §12 rule) and ageing."""
    since = _window_start(days)
    now_utc = datetime.utcnow()
    pos = db.execute(
        select(VendorPurchaseOrder).where(VendorPurchaseOrder.created_at >= since)
    ).scalars().all()

    # Delivery completeness per (vendor, part), from the authoritative source.
    tracking_by_key = {
        (row.vendor_id, row.part_id): row
        for row in delivery_tracking_service.compute_rows(db)
    }

    status_counts = {"GENERATED": 0, "EMAILED": 0, "EMAIL_FAILED": 0}
    complete = partial = not_supplied = overdue = 0
    due_hours = _po_invoice_due_hours()
    ageing = {"0-1d": 0, "2-3d": 0, "4-7d": 0, "8-15d": 0, "15d+": 0}
    for po in pos:
        age_hours_total = (now_utc - po.created_at).total_seconds() / 3600
        if age_hours_total > due_hours and not _po_is_invoiced(po, db):
            overdue += 1
        status_counts[po.status.value] = status_counts.get(po.status.value, 0) + 1
        items = db.execute(
            select(VendorPurchaseOrderItem).where(
                VendorPurchaseOrderItem.purchase_order_id == po.id
            )
        ).scalars().all()
        delivered_all = bool(items)
        has_delivery = False
        for item in items:
            row = tracking_by_key.get((po.vendor_id, item.part_id))
            if row is not None and row.delivered_qty > 0:
                has_delivery = True
            if row is None or row.delivered_qty < row.ordered_qty:
                delivered_all = False
        if items and delivered_all:
            complete += 1
        elif has_delivery:
            partial += 1
        else:
            not_supplied += 1
            age_days = (now_utc - po.created_at).total_seconds() / 86400
            if age_days <= 1:
                ageing["0-1d"] += 1
            elif age_days <= 3:
                ageing["2-3d"] += 1
            elif age_days <= 7:
                ageing["4-7d"] += 1
            elif age_days <= 15:
                ageing["8-15d"] += 1
            else:
                ageing["15d+"] += 1

    return PoTower(
        created_in_window=len(pos),
        generated=status_counts.get("GENERATED", 0),
        emailed=status_counts.get("EMAILED", 0),
        email_failed=status_counts.get("EMAIL_FAILED", 0),
        complete=complete,
        partial=partial,
        not_supplied=not_supplied,
        overdue=overdue,
        ageing=ageing,
    )


class DeliveryTower(BaseModel):
    ordered_qty: float
    delivered_qty: float
    short_qty: float
    fulfilment_pct: float | None
    complete_lines: int
    partial_lines: int
    not_delivered_lines: int
    vendor_short: list[dict]  # {vendor, short_qty}
    part_short: list[dict]  # {part, short_qty}
    daily: list[dict]  # {date, delivered_qty}


@router.get("/delivery-tower", response_model=DeliveryTower)
def command_centre_delivery_tower(days: int = 30, db: Session = Depends(get_db)) -> DeliveryTower:
    """Delivery Control Tower (spec §12) -- entirely from the existing
    Delivery Tracking computation, never a parallel calculation."""
    rows = delivery_tracking_service.compute_rows(db)
    summary = delivery_tracking_service.compute_summary(rows)

    vendor_short: dict[str, Decimal] = {}
    part_short: dict[str, Decimal] = {}
    for row in rows:
        if row.short_qty > 0:
            vendor_short[row.vendor_name] = vendor_short.get(row.vendor_name, Decimal(0)) + row.short_qty
            part_short[row.part_number] = part_short.get(row.part_number, Decimal(0)) + row.short_qty

    since_date = (now_ist() - timedelta(days=days)).date()
    daily = delivery_tracking_service.compute_daily_deliveries(db, date_from=since_date)

    total_ordered = float(summary.total_ordered_qty)
    return DeliveryTower(
        ordered_qty=total_ordered,
        delivered_qty=float(summary.total_delivered_qty),
        short_qty=float(summary.total_short_qty),
        fulfilment_pct=round(float(summary.total_delivered_qty) / total_ordered * 100, 1)
        if total_ordered
        else None,
        complete_lines=summary.complete_count,
        partial_lines=summary.partial_count,
        not_delivered_lines=summary.not_delivered_count,
        vendor_short=[
            {"vendor": name, "short_qty": float(qty)}
            for name, qty in sorted(vendor_short.items(), key=lambda kv: -kv[1])[:10]
        ],
        part_short=[
            {"part": part, "short_qty": float(qty)}
            for part, qty in sorted(part_short.items(), key=lambda kv: -kv[1])[:10]
        ],
        daily=[
            {"date": point.delivery_date.isoformat(), "delivered_qty": float(point.delivered_qty)}
            for point in daily
        ],
    )


class VendorScore(BaseModel):
    vendor_id: int
    vendor: str
    vendor_code: str | None
    declared_qty: float
    allocated_qty: float
    invoiced_qty: float
    delivered_qty: float
    short_qty: float
    fulfilment_pct: float | None  # delivered vs allocated
    trust_pct: float | None  # spec §10: does supply match declaration chain?
    submissions_30d: int
    discipline_pct: float | None  # submission days / 30
    score: float | None  # weighted composite, /100
    rank: int | None = None


@router.get("/vendor-scorecard", response_model=list[VendorScore])
def command_centre_vendor_scorecard(db: Session = Depends(get_db)) -> list[VendorScore]:
    """Vendor scorecard + stock-trust (spec §9, §10). The composite score
    uses the spec's weights RENORMALISED over the metrics that exist today
    (price competitiveness and due-date timeliness have no data yet):
    fulfilment 25, trust 25, availability 10, discipline 5 -> scaled to 100.
    A vendor with no allocations yet gets no score, never a fake 0."""
    month_ago = datetime.utcnow() - timedelta(days=30)

    declared = dict(db.execute(
        select(VendorInventory.vendor_id,
               func.coalesce(func.sum(VendorInventory.quantity_available), 0))
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .where(InventoryImport.is_active.is_(True))
        .group_by(VendorInventory.vendor_id)
    ).all())
    allocated = dict(db.execute(
        select(VendorSelection.vendor_id,
               func.coalesce(func.sum(VendorSelection.quantity_selected), 0))
        .group_by(VendorSelection.vendor_id)
    ).all())
    invoiced = dict(db.execute(
        select(VendorInvoiceImport.vendor_id,
               func.coalesce(func.sum(VendorInvoiceLineResult.quantity_invoiced), 0))
        .join(VendorInvoiceLineResult,
              VendorInvoiceLineResult.invoice_import_id == VendorInvoiceImport.id)
        .where(VendorInvoiceImport.vendor_id.isnot(None))
        .group_by(VendorInvoiceImport.vendor_id)
    ).all())
    submissions = dict(db.execute(
        select(InventoryImport.vendor_id, func.count(func.distinct(
            func.date(InventoryImport.created_at))))
        .where(InventoryImport.created_at >= month_ago)
        .group_by(InventoryImport.vendor_id)
    ).all())

    tracking_rows = delivery_tracking_service.compute_rows(db)
    delivered: dict[int, Decimal] = {}
    short: dict[int, Decimal] = {}
    for row in tracking_rows:
        delivered[row.vendor_id] = delivered.get(row.vendor_id, Decimal(0)) + row.delivered_qty
        short[row.vendor_id] = short.get(row.vendor_id, Decimal(0)) + row.short_qty

    max_declared = max((float(v) for v in declared.values()), default=0.0)

    vendors = db.execute(select(Vendor).order_by(Vendor.name)).scalars().all()
    scores: list[VendorScore] = []
    for vendor in vendors:
        d = float(declared.get(vendor.id, 0))
        a = float(allocated.get(vendor.id, 0))
        inv = float(invoiced.get(vendor.id, 0))
        dl = float(delivered.get(vendor.id, 0))
        sh = float(short.get(vendor.id, 0))
        subs = int(submissions.get(vendor.id, 0))
        if d == 0 and a == 0 and dl == 0:
            continue  # nothing to judge -- omit rather than fake

        fulfilment = round(min(dl / a, 1.0) * 100, 1) if a else None
        # Trust (§10): of what we allocated against their declaration, how
        # much actually arrived (invoiced counts when no delivery yet).
        arrived = max(dl, inv)
        trust = round(min(arrived / a, 1.0) * 100, 1) if a else None
        availability = round(d / max_declared * 100, 1) if max_declared else None
        discipline = round(min(subs / 30, 1.0) * 100, 1)

        weighted: list[tuple[float, float]] = []  # (weight, value)
        if fulfilment is not None:
            weighted.append((25, fulfilment))
        if trust is not None:
            weighted.append((25, trust))
        if availability is not None:
            weighted.append((10, availability))
        weighted.append((5, discipline))
        total_weight = sum(w for w, _ in weighted)
        score = round(sum(w * v for w, v in weighted) / total_weight, 1) if total_weight else None

        scores.append(VendorScore(
            vendor_id=vendor.id,
            vendor=vendor.name,
            vendor_code=vendor.vendor_code,
            declared_qty=d,
            allocated_qty=a,
            invoiced_qty=inv,
            delivered_qty=dl,
            short_qty=sh,
            fulfilment_pct=fulfilment,
            trust_pct=trust,
            submissions_30d=subs,
            discipline_pct=discipline,
            score=score,
        ))

    scores.sort(key=lambda s: (s.score is None, -(s.score or 0)))
    for index, entry in enumerate(scores):
        if entry.score is not None:
            entry.rank = index + 1
    return scores


class TrendPoint(BaseModel):
    date: str
    orders_qty: float
    allocated_qty: float
    stock_qty: float
    fill_rate_pct: float | None
    files_failed: int


@router.get("/trends", response_model=list[TrendPoint])
def command_centre_trends(days: int = 30, db: Session = Depends(get_db)) -> list[TrendPoint]:
    """Daily management trends (spec §21): demand, allocation, fill rate,
    incoming stock, failures -- one grouped query per series."""
    since = _window_start(days)

    ordered_by_day = {
        str(day): float(qty)
        for day, qty in db.execute(
            select(func.date(CustomerOrder.created_at),
                   func.coalesce(func.sum(CustomerOrderItem.quantity_requested), 0))
            .join(CustomerOrderItem, CustomerOrderItem.customer_order_id == CustomerOrder.id)
            .where(CustomerOrder.created_at >= since)
            .group_by(func.date(CustomerOrder.created_at))
        )
    }
    allocated_by_day = {
        str(day): float(qty)
        for day, qty in db.execute(
            select(func.date(CustomerOrder.created_at),
                   func.coalesce(func.sum(VendorSelection.quantity_selected), 0))
            .join(CustomerOrderItem, CustomerOrderItem.customer_order_id == CustomerOrder.id)
            .join(VendorSelection,
                  VendorSelection.customer_order_item_id == CustomerOrderItem.id)
            .where(CustomerOrder.created_at >= since)
            .group_by(func.date(CustomerOrder.created_at))
        )
    }
    stock_by_day = {
        str(day): float(qty)
        for day, qty in db.execute(
            select(func.date(InventoryImport.created_at),
                   func.coalesce(func.sum(VendorInventory.quantity_available), 0))
            .join(VendorInventory, VendorInventory.import_id == InventoryImport.id)
            .where(InventoryImport.created_at >= since)
            .group_by(func.date(InventoryImport.created_at))
        )
    }
    failed_by_day = {
        str(day): count
        for day, count in db.execute(
            select(func.date(IncomingDocument.received_at), func.count())
            .where(IncomingDocument.received_at >= since,
                   IncomingDocument.status.in_(["FAILED", "DOWNLOAD_FAILED"]))
            .group_by(func.date(IncomingDocument.received_at))
        )
    }

    points: list[TrendPoint] = []
    day = since.date()
    end = datetime.utcnow().date()
    while day <= end:
        key = day.isoformat()
        ordered = ordered_by_day.get(key, 0.0)
        allocated_qty = allocated_by_day.get(key, 0.0)
        points.append(TrendPoint(
            date=key,
            orders_qty=ordered,
            allocated_qty=allocated_qty,
            stock_qty=stock_by_day.get(key, 0.0),
            fill_rate_pct=round(allocated_qty / ordered * 100, 1) if ordered else None,
            files_failed=failed_by_day.get(key, 0),
        ))
        day += timedelta(days=1)
    return points


# ===========================================================================
# Phase 3 -- Part Intelligence (§18), Price Leakage / Finance-lite (§14-§15),
# Audit trail viewer (§24). Money figures are shown ONLY where the vendor's
# own file carried a price, always with explicit coverage so a partially-
# priced total can never masquerade as a complete one (spec §28).
# ===========================================================================


class PartVendorRow(BaseModel):
    vendor_id: int
    vendor: str
    vendor_code: str | None
    vendor_part_number: str
    declared: float
    reserved: float
    live_remaining: float
    price: float | None
    mrp: float | None
    is_selected: bool
    delivered: float
    delivery_short: float
    last_upload: str | None


class PartIntelligence(BaseModel):
    query: str
    canonical_part_number: str | None
    aliases: list[str]
    description: str | None
    brand: str | None
    vendors: list[PartVendorRow]
    demand_30d: float
    allocated_30d: float
    short_30d: float
    best_price: float | None
    selected_price_max: float | None
    price_note: str


@router.get("/part-intelligence", response_model=PartIntelligence)
def command_centre_part_intelligence(q: str, db: Session = Depends(get_db)) -> PartIntelligence:
    """One consolidated intelligence page for any part number (spec §18):
    identity + aliases, every vendor's stock/reservations/price, open
    demand, and price positioning -- all through the same alias-aware
    matching the allocation engine uses."""
    normalized = normalise_part_number(q)
    numbers = _matchable_part_numbers(normalized, db)

    from core.models import Part, PartAlias

    parts = db.execute(
        select(Part).where(Part.canonical_part_number.in_(numbers))
    ).scalars().all()
    part_ids = [part.id for part in parts]
    aliases = sorted({
        alias for alias in db.execute(
            select(PartAlias.vendor_part_number).where(PartAlias.part_id.in_(part_ids))
        ).scalars()
    }) if part_ids else []

    stock_rows = db.execute(
        select(VendorInventory, Vendor, InventoryImport)
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .join(Vendor, VendorInventory.vendor_id == Vendor.id)
        .where(
            VendorInventory.normalized_part_number.in_(numbers),
            InventoryImport.is_active.is_(True),
        )
    ).all()

    selected_vendor_ids = set(db.execute(
        select(func.distinct(VendorSelection.vendor_id)).where(
            VendorSelection.part_id.in_(part_ids)
        )
    ).scalars()) if part_ids else set()

    tracking_by_key = {
        (row.vendor_id, row.part_id): row
        for row in delivery_tracking_service.compute_rows(db)
        if row.part_id in part_ids
    }

    vendors: list[PartVendorRow] = []
    for inventory_row, vendor, import_row in stock_rows:
        reserved = db.execute(
            select(func.coalesce(func.sum(VendorSelection.quantity_selected), 0)).where(
                VendorSelection.vendor_id == inventory_row.vendor_id,
                VendorSelection.part_id == inventory_row.part_id,
            )
        ).scalar_one()
        tracking = tracking_by_key.get((vendor.id, inventory_row.part_id))
        vendors.append(PartVendorRow(
            vendor_id=vendor.id,
            vendor=vendor.name,
            vendor_code=vendor.vendor_code,
            vendor_part_number=inventory_row.vendor_part_number,
            declared=float(inventory_row.quantity_available),
            reserved=float(reserved),
            live_remaining=float(Decimal(inventory_row.quantity_available) - Decimal(reserved)),
            price=float(inventory_row.price) if inventory_row.price is not None else None,
            mrp=float(inventory_row.mrp) if inventory_row.mrp is not None else None,
            is_selected=vendor.id in selected_vendor_ids,
            delivered=float(tracking.delivered_qty) if tracking else 0.0,
            delivery_short=float(tracking.short_qty) if tracking else 0.0,
            last_upload=import_row.created_at.strftime("%d %b %Y") if import_row.created_at else None,
        ))
    vendors.sort(key=lambda row: -row.declared)

    month_ago = datetime.utcnow() - timedelta(days=30)
    demand = allocated = Decimal(0)
    items = db.execute(
        select(CustomerOrderItem)
        .join(CustomerOrder, CustomerOrderItem.customer_order_id == CustomerOrder.id)
        .where(CustomerOrder.created_at >= month_ago)
    ).scalars().all()
    for item in items:
        if normalise_part_number(item.part_number_raw) not in numbers:
            continue
        demand += Decimal(item.quantity_requested)
        allocated += db.execute(
            select(func.coalesce(func.sum(VendorSelection.quantity_selected), 0)).where(
                VendorSelection.customer_order_item_id == item.id
            )
        ).scalar_one()

    priced = [row.price for row in vendors if row.price is not None]
    selected_prices = [
        row.price for row in vendors if row.is_selected and row.price is not None
    ]
    price_note = (
        f"Prices known for {len(priced)} of {len(vendors)} vendor(s)."
        if vendors
        else "No vendor currently stocks this part."
    )

    part = parts[0] if parts else None
    return PartIntelligence(
        query=q,
        canonical_part_number=part.canonical_part_number if part else None,
        aliases=aliases,
        description=part.description if part else None,
        brand=part.brand if part else None,
        vendors=vendors,
        demand_30d=float(demand),
        allocated_30d=float(allocated),
        short_30d=float(demand - allocated),
        best_price=min(priced) if priced else None,
        selected_price_max=max(selected_prices) if selected_prices else None,
        price_note=price_note,
    )


class LeakageRow(BaseModel):
    part_number: str
    selected_vendor: str
    selected_price: float
    best_vendor: str
    best_price: float
    quantity: float
    variance: float
    leakage: float


class PriceLeakage(BaseModel):
    window_days: int
    allocated_qty_total: float
    allocated_qty_priced: float
    priced_coverage_pct: float | None
    purchase_value_priced: float
    potential_leakage: float
    rows: list[LeakageRow]
    note: str


@router.get("/price-leakage", response_model=PriceLeakage)
def command_centre_price_leakage(days: int = 30, db: Session = Depends(get_db)) -> PriceLeakage:
    """Spec §15: for allocations where the SELECTED vendor's price is known,
    compare against the best-priced ACTIVE alternative for the same part.
    Every rupee figure carries its coverage -- allocations without price data
    are counted and reported, never silently included at ₹0."""
    since = _window_start(days)

    selections = db.execute(
        select(VendorSelection, Vendor)
        .join(Vendor, VendorSelection.vendor_id == Vendor.id)
        .join(CustomerOrderItem,
              VendorSelection.customer_order_item_id == CustomerOrderItem.id)
        .join(CustomerOrder, CustomerOrderItem.customer_order_id == CustomerOrder.id)
        .where(CustomerOrder.created_at >= since)
    ).all()

    # Active price per (vendor, part) + best price per part -- two passes
    # over one query.
    price_rows = db.execute(
        select(VendorInventory.vendor_id, VendorInventory.part_id,
               VendorInventory.price, VendorInventory.vendor_part_number, Vendor.name)
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .join(Vendor, VendorInventory.vendor_id == Vendor.id)
        .where(InventoryImport.is_active.is_(True))
    ).all()
    price_by_vendor_part: dict[tuple[int, int], Decimal] = {}
    part_number_by_part: dict[int, str] = {}
    best_by_part: dict[int, tuple[Decimal, str]] = {}
    for vendor_id, part_id, price, raw_number, vendor_name in price_rows:
        part_number_by_part.setdefault(part_id, raw_number)
        if price is None:
            continue
        price_by_vendor_part[(vendor_id, part_id)] = Decimal(price)
        best = best_by_part.get(part_id)
        if best is None or Decimal(price) < best[0]:
            best_by_part[part_id] = (Decimal(price), vendor_name)

    total_qty = priced_qty = Decimal(0)
    purchase_value = leakage_total = Decimal(0)
    rows: list[LeakageRow] = []
    for selection, vendor in selections:
        qty = Decimal(selection.quantity_selected)
        total_qty += qty
        selected_price = price_by_vendor_part.get((selection.vendor_id, selection.part_id))
        if selected_price is None:
            continue
        priced_qty += qty
        purchase_value += selected_price * qty
        best = best_by_part.get(selection.part_id)
        if best is None:
            continue
        best_price, best_vendor = best
        if selected_price > best_price:
            variance = selected_price - best_price
            leakage = variance * qty
            leakage_total += leakage
            rows.append(LeakageRow(
                part_number=part_number_by_part.get(selection.part_id, str(selection.part_id)),
                selected_vendor=vendor.name,
                selected_price=float(selected_price),
                best_vendor=best_vendor,
                best_price=float(best_price),
                quantity=float(qty),
                variance=float(variance),
                leakage=float(leakage),
            ))

    rows.sort(key=lambda row: -row.leakage)
    coverage = (
        round(float(priced_qty / total_qty * 100), 1) if total_qty else None
    )
    return PriceLeakage(
        window_days=days,
        allocated_qty_total=float(total_qty),
        allocated_qty_priced=float(priced_qty),
        priced_coverage_pct=coverage,
        purchase_value_priced=float(purchase_value),
        potential_leakage=float(leakage_total),
        rows=rows[:25],
        note=(
            "Value figures cover ONLY allocations whose selected vendor "
            "declared a price in their stock file"
            + (f" ({coverage}% of allocated quantity)." if coverage is not None else ".")
        ),
    )


class TeamActivityRow(BaseModel):
    number: str
    name: str  # team member name / vendor / customer / "unknown"
    kind: str  # "team" | "vendor" | "customer" | "admin" | "unknown"
    files_sent: int
    orders_sent: int
    stock_files_sent: int
    failed_files: int
    last_activity: str | None


@router.get("/team-activity", response_model=list[TeamActivityRow])
def command_centre_team_activity(days: int = 30, db: Session = Depends(get_db)) -> list[TeamActivityRow]:
    """WHO is sending work through WhatsApp (Founder's clarification:
    number-wise counts per person -- Alam, Rajkumar, ...). Senders are
    labelled from the purchase-team list, the vendor/customer registry and
    the admin numbers; the Founder is the only web user, so this page is
    inherently founder-only today."""
    from backend.app.integrations.whatsapp.models import PurchaseTeamMember

    since = _window_start(days)
    documents = db.execute(
        select(IncomingDocument).where(
            IncomingDocument.received_at >= since,
            IncomingDocument.sender.isnot(None),
        )
    ).scalars().all()

    team = {
        registry.normalize_number(member.whatsapp_number): member.name
        for member in db.execute(select(PurchaseTeamMember)).scalars()
    }
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    admin_numbers = {
        registry.normalize_number(number)
        for number in whatsapp_settings.admin_phone_numbers
    }

    stats: dict[str, dict] = {}
    for document in documents:
        number = registry.normalize_number(document.sender)
        if not number:
            continue
        entry = stats.setdefault(
            number,
            {"files": 0, "orders": 0, "stock": 0, "failed": 0, "last": None},
        )
        entry["files"] += 1
        doc_type = document.document_type.value
        if doc_type == "CUSTOMER_ORDER":
            entry["orders"] += 1
        elif doc_type == "VENDOR_INVENTORY":
            entry["stock"] += 1
        if document.status.value in ("FAILED", "DOWNLOAD_FAILED"):
            entry["failed"] += 1
        if entry["last"] is None or (
            document.received_at and document.received_at > entry["last"]
        ):
            entry["last"] = document.received_at

    rows: list[TeamActivityRow] = []
    for number, entry in stats.items():
        if number in team:
            name, kind = team[number], "team"
        elif number in admin_numbers:
            name, kind = "Founder/Admin", "admin"
        else:
            party = registry.lookup(number, db)
            if party is not None:
                name, kind = party.name, party.party_type
            else:
                name, kind = "unknown", "unknown"
        rows.append(TeamActivityRow(
            number=number,
            name=name,
            kind=kind,
            files_sent=entry["files"],
            orders_sent=entry["orders"],
            stock_files_sent=entry["stock"],
            failed_files=entry["failed"],
            last_activity=entry["last"].isoformat() if entry["last"] else None,
        ))
    rows.sort(key=lambda row: -row.files_sent)
    return rows


class AuditRow(BaseModel):
    id: int
    actor: str
    action: str
    entity_type: str
    entity_id: str | None
    previous_value: str | None
    new_value: str | None
    reason: str | None
    created_at: str


@router.get("/audit", response_model=list[AuditRow])
def command_centre_audit(limit: int = 100, db: Session = Depends(get_db)) -> list[AuditRow]:
    """Spec §24: the manual-override trail, newest first."""
    from backend.app.services import audit_service

    return [
        AuditRow(
            id=row.id,
            actor=row.actor,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            previous_value=row.previous_value,
            new_value=row.new_value,
            reason=row.reason,
            created_at=row.created_at.isoformat() if row.created_at else "",
        )
        for row in audit_service.list_recent(db, limit=min(limit, 500))
    ]


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
