"""Auto top-up: when a vendor uploads NEW stock, fill the unfilled parts of
recent customer orders from it -- WITHOUT ever moving an allocation that
already exists.

The Founder's rule (chosen deliberately over a full re-run): an allocation
that has already been communicated to a vendor must never silently change.
So this only ever ADDS quantity to a line that is still short:

    line requested 20, already allocated 12  ->  shortfall 8
    the newly-uploaded vendor has 5 remaining ->  +5 (now 17, still short 3)

Existing `VendorSelection` rows are never edited, never deleted, never
re-pointed to another vendor. Every write goes through the SAME
`vendor_selection_service.upsert_selection` the manual and automatic paths
use, so the reservation ledger, the row lock, and the "never exceed what the
vendor has remaining / what the line requested" guards all apply unchanged.

Scope guard: only orders created within `TOPUP_WINDOW_DAYS` (default 7) are
considered, so a months-old order can never be quietly modified.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.ingestion.column_detector import normalise_part_number
from core.logging_setup import get_logger
from core.models import (
    Customer,
    CustomerOrder,
    CustomerOrderItem,
    InventoryImport,
    VendorInventory,
    VendorSelection,
)
from core.services import vendor_selection_service, vendor_stock_service

logger = get_logger(__name__)


def topup_enabled() -> bool:
    """`TOPUP_ON_NEW_STOCK=false` disables the whole feature."""
    return os.environ.get("TOPUP_ON_NEW_STOCK", "true").strip().lower() == "true"


def topup_window_days() -> int:
    try:
        return max(0, int(os.environ.get("TOPUP_WINDOW_DAYS", "7")))
    except ValueError:
        return 7


@dataclass
class TopUpLine:
    order_id: int
    customer_name: str | None
    part_number: str
    added_quantity: Decimal
    still_short: Decimal


@dataclass
class TopUpResult:
    vendor_id: int
    vendor_name: str | None = None
    lines: list[TopUpLine] = field(default_factory=list)

    @property
    def total_added(self) -> Decimal:
        return sum((line.added_quantity for line in self.lines), Decimal(0))

    @property
    def order_ids(self) -> list[int]:
        seen: list[int] = []
        for line in self.lines:
            if line.order_id not in seen:
                seen.append(line.order_id)
        return seen

    def summary_lines(self) -> list[str]:
        """Human-readable, grouped by order: 'Order 12 — Karol Bagh: +5 P-1001'."""
        by_order: dict[int, list[TopUpLine]] = {}
        for line in self.lines:
            by_order.setdefault(line.order_id, []).append(line)
        out: list[str] = []
        for order_id, lines in by_order.items():
            who = lines[0].customer_name or "customer not identified"
            parts = ", ".join(
                f"+{_plain(line.added_quantity)} {line.part_number}" for line in lines
            )
            short = sum((line.still_short for line in lines), Decimal(0))
            text = f"Order {order_id} — {who}: {parts}"
            if short > 0:
                text += f" (still short {_plain(short)})"
            out.append(text)
        return out


def _plain(value: Decimal) -> str:
    """'5' not '5.0000' -- these strings go into WhatsApp messages."""
    normalized = value.normalize()
    return format(normalized, "f")


def _vendor_offer_parts(vendor_id: int, session: Session) -> dict[str, VendorInventory]:
    """This vendor's ACTIVE stock, keyed by normalized part number."""
    rows = session.execute(
        select(VendorInventory)
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .where(
            VendorInventory.vendor_id == vendor_id,
            InventoryImport.is_active.is_(True),
        )
    ).scalars()
    return {row.normalized_part_number: row for row in rows}


def find_shortfall_items(session: Session, *, window_days: int) -> list[CustomerOrderItem]:
    """Recent order lines whose allocated quantity is still below what the
    customer requested (including lines with no allocation at all)."""
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    allocated = (
        select(
            VendorSelection.customer_order_item_id.label("item_id"),
            func.coalesce(func.sum(VendorSelection.quantity_selected), 0).label("qty"),
        )
        .group_by(VendorSelection.customer_order_item_id)
        .subquery()
    )
    rows = session.execute(
        select(CustomerOrderItem)
        .join(CustomerOrder, CustomerOrderItem.customer_order_id == CustomerOrder.id)
        .outerjoin(allocated, allocated.c.item_id == CustomerOrderItem.id)
        .where(
            CustomerOrder.created_at >= cutoff,
            CustomerOrderItem.quantity_requested
            > func.coalesce(allocated.c.qty, 0),
        )
        .order_by(CustomerOrderItem.customer_order_id, CustomerOrderItem.row_number)
    ).scalars()
    return list(rows)


def top_up_from_vendor(vendor_id: int, session: Session) -> TopUpResult:
    """Fill recent shortfalls from this vendor's freshly-imported stock.
    Adds only; never touches an existing allocation. Never raises."""
    result = TopUpResult(vendor_id=vendor_id)
    if not topup_enabled():
        return result

    offers = _vendor_offer_parts(vendor_id, session)
    if not offers:
        return result

    for item in find_shortfall_items(session, window_days=topup_window_days()):
        normalized = normalise_part_number(item.part_number_raw)
        offer = offers.get(normalized)
        if offer is None:
            continue

        already = sum(
            (
                selection.quantity_selected
                for selection in vendor_selection_service.list_selections_for_item(
                    item.id, session
                )
            ),
            Decimal(0),
        )
        shortfall = Decimal(item.quantity_requested) - already
        if shortfall <= 0:
            continue

        # What this vendor can still give, after every OTHER order's
        # reservations -- the same live figure the comparison engine uses.
        remaining = vendor_stock_service.remaining_quantity(
            vendor_id,
            offer.part_id,
            offer.quantity_available,
            session,
            exclude_order_item_id=item.id,
        )
        # This vendor may already be supplying part of THIS line; adding to it
        # means re-upserting the combined figure (upsert replaces that
        # vendor's own allocation for the line -- never another vendor's).
        existing_from_vendor = sum(
            (
                selection.quantity_selected
                for selection in vendor_selection_service.list_selections_for_item(
                    item.id, session
                )
                if selection.vendor_id == vendor_id
            ),
            Decimal(0),
        )
        addable = min(shortfall, max(remaining - existing_from_vendor, Decimal(0)))
        if addable <= 0:
            continue

        try:
            vendor_selection_service.upsert_selection(
                item.id, vendor_id, existing_from_vendor + addable, session
            )
        except (ValueError, LookupError) as exc:
            # Lost a race (another order took the stock first) or a guard
            # rejected it -- skip this line, never fail the import.
            logger.info(
                "Top-up skipped for order item %s from vendor %s: %s", item.id, vendor_id, exc
            )
            continue

        order = session.get(CustomerOrder, item.customer_order_id)
        customer_name = None
        if order is not None and order.customer_id is not None:
            customer = session.get(Customer, order.customer_id)
            customer_name = customer.name if customer is not None else None
        result.lines.append(
            TopUpLine(
                order_id=item.customer_order_id,
                customer_name=customer_name,
                part_number=item.part_number_raw,
                added_quantity=addable,
                still_short=shortfall - addable,
            )
        )
        logger.info(
            "Top-up: order %s line %s +%s of %s from vendor %s.",
            item.customer_order_id,
            item.id,
            addable,
            item.part_number_raw,
            vendor_id,
        )

    return result
