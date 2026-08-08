"""Live remaining-stock computation -- the fix for the confirmed
double-allocation gap: a vendor's real remaining quantity for a part is its
currently-imported quantity minus the sum of every `VendorSelection` already
reserved against that same vendor+part, across ALL customer orders (not just
one order line, which is all the previous check considered).

Deliberately a live SQL SUM on every call, NOT a decrementing counter column
on `VendorInventory` -- re-running or removing a selection
(`vendor_selection_service.clear_selections_for_item` / `remove_selection`,
or a cascade-delete of a whole customer order) is a plain row DELETE, so the
very next SUM instantly reflects the released quantity with zero extra
bookkeeping. `VendorInventory.quantity_available` itself is never written to
after import -- see `core.services.inventory_import_service`.

No dependency on `vendor_comparison_service` or `vendor_selection_service`
(only on `core.models`) so both of those can import this without a cycle --
`vendor_selection_service` already imports `vendor_comparison_service` for
its export report.

Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import VendorSelection


def reserved_quantity(
    vendor_id: int,
    part_id: int,
    session: Session,
    *,
    exclude_order_item_id: int | None = None,
) -> Decimal:
    """Sum of `quantity_selected` for every `VendorSelection` currently held
    against (vendor_id, part_id), across every customer order line -- the
    vendor's total real-world commitment for this part right now, regardless
    of `status` (SELECTED and ORDERED are equally spent stock; a Purchase
    Order is a paperwork step, not a new commitment).

    `exclude_order_item_id`, when given, excludes that ONE order line's own
    existing selection(s) for this vendor -- used when re-selecting the same
    vendor for the same line (upsert), since that old row is about to be
    replaced, not double-counted alongside the new value.
    """
    statement = select(func.coalesce(func.sum(VendorSelection.quantity_selected), 0)).where(
        VendorSelection.vendor_id == vendor_id,
        VendorSelection.part_id == part_id,
    )
    if exclude_order_item_id is not None:
        statement = statement.where(
            VendorSelection.customer_order_item_id != exclude_order_item_id
        )
    return Decimal(str(session.execute(statement).scalar_one()))


def remaining_quantity(
    vendor_id: int,
    part_id: int,
    raw_available: Decimal,
    session: Session,
    *,
    exclude_order_item_id: int | None = None,
) -> Decimal:
    """`raw_available` (the vendor's currently-imported quantity) minus
    everything already reserved against it elsewhere. Can be negative if
    pre-existing data was over-allocated before this check existed --
    callers building a customer-facing display should clamp that to 0;
    callers enforcing the allocation cap (`vendor_selection_service.
    upsert_selection`) should use the signed value as-is, since any positive
    `quantity_selected` request is correctly rejected against a negative
    remaining."""
    reserved = reserved_quantity(
        vendor_id, part_id, session, exclude_order_item_id=exclude_order_item_id
    )
    return raw_available - reserved
