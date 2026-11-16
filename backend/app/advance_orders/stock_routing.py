"""Case 2: which vendor to ORDER a part from, when vendors have it in stock.

The sales bot found the part as dealer stock on the Dealer Portal -- stock
ProcureHub put there from the vendors' own stock files. So ProcureHub knows
who holds it, and asks them first, in this order:

  1. the vendor behind the Portal dealer the sales bot named (`dealer_id`),
     when it sent one -- that is whose stock the customer was shown;
  2. every other vendor still holding it, most stock first.

Only after all of them say no does the order go to the brand's vendor list
(VENDOR BRAND MAPPING.xlsx), exactly as case 3 does.

"Still holding it" is the vendor's imported quantity, minus what customer
orders have reserved against it (the same reservation ledger allocation
uses), minus what earlier case-2 orders have already ordered from him since
his current stock file arrived -- so the same 5 pieces are never promised to
two customers. The vendor's next stock file starts the count again.

The same physical part under another number counts: a vendor's "Root Part
Num" / OEM number, and Founder-declared equivalences, are matched exactly as
allocation matches them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as m
from core.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class StockHolder:
    vendor_id: int
    vendor_name: str
    remaining: Decimal
    named_by_sales_bot: bool = False


def _matchable(part_number: str, session: Session) -> list[str]:
    from core.ingestion.column_detector import normalise_part_number
    from core.services.vendor_selection_service import _matchable_part_numbers

    key = normalise_part_number(part_number)
    return _matchable_part_numbers(key, session) if key else []


def _vendor_for_dealer(dealer_id: int | None, session: Session) -> int | None:
    if not dealer_id:
        return None
    from core.models import DealerPortalVendorMap

    return session.execute(
        select(DealerPortalVendorMap.vendor_id).where(DealerPortalVendorMap.dp_dealer_id == int(dealer_id))
    ).scalars().first()


def _already_ordered(vendor_id: int, numbers: list[str], stock_import_id: int, session: Session, exclude_line_id: int | None) -> Decimal:
    """Pieces earlier case-2 orders have ordered from this vendor for this
    part against his CURRENT stock file."""
    from core.ingestion.column_detector import normalise_part_number

    rows = session.execute(
        select(m.AdvanceOrderLine.part_number, m.AdvanceOrderLine.available_qty, m.AdvanceOrderLine.qty, m.AdvanceOrderLine.id)
        .join(m.AdvanceOrder, m.AdvanceOrder.id == m.AdvanceOrderLine.advance_order_id)
        .where(
            m.AdvanceOrder.kind == m.KIND_DEALER_STOCK,
            m.AdvanceOrderLine.vendor_id == vendor_id,
            m.AdvanceOrderLine.status == m.LINE_ORDERED,
            m.AdvanceOrderLine.stock_import_id == stock_import_id,
        )
    ).all()
    wanted = set(numbers)
    total = Decimal(0)
    for part, available, qty, line_id in rows:
        if line_id == exclude_line_id:
            continue
        if normalise_part_number(part) in wanted:
            total += Decimal(available or qty or 0)
    return total


def active_import_id(vendor_id: int, session: Session) -> int | None:
    from core.models import InventoryImport

    return session.execute(
        select(InventoryImport.id).where(InventoryImport.vendor_id == vendor_id, InventoryImport.is_active.is_(True))
    ).scalars().first()


def stock_holders(line: m.AdvanceOrderLine, session: Session) -> list[StockHolder]:
    """Vendors holding this line's part right now, in the order to ask them."""
    from core.models import InventoryImport, Vendor, VendorInventory
    from core.services.own_stock import is_own_stock_vendor
    from core.services.vendor_stock_service import reserved_quantity

    numbers = _matchable(line.part_number, session)
    if not numbers:
        return []
    rows = session.execute(
        select(VendorInventory, InventoryImport.id, Vendor)
        .join(InventoryImport, InventoryImport.id == VendorInventory.import_id)
        .join(Vendor, Vendor.id == VendorInventory.vendor_id)
        .where(
            InventoryImport.is_active.is_(True),
            VendorInventory.normalized_part_number.in_(numbers),
            VendorInventory.quantity_available > 0,
            Vendor.active.is_(True),
        )
    ).all()
    named = _vendor_for_dealer(line.dealer_id, session)
    best: dict[int, StockHolder] = {}
    for row, import_id, vendor in rows:
        # The company's own warehouses are not dealer stock: the sales bot
        # already looked there before it came to us.
        if is_own_stock_vendor(vendor.name, flag=bool(vendor.is_own_stock)):
            continue
        free = Decimal(row.quantity_available)
        if row.part_id is not None:
            free -= reserved_quantity(vendor.id, row.part_id, session)
        free -= _already_ordered(vendor.id, numbers, import_id, session, line.id)
        if free <= 0:
            continue
        current = best.get(vendor.id)
        if current is None or free > current.remaining:
            best[vendor.id] = StockHolder(vendor.id, vendor.name, free, vendor.id == named)
    return sorted(best.values(), key=lambda h: (not h.named_by_sales_bot, -h.remaining, h.vendor_name))
