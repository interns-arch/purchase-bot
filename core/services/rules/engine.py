"""Automatic Vendor Selection: ONE automatic action, no strategy choice.

For each customer order line it applies allocations via the SAME
`vendor_selection_service.upsert_selection` manual selection uses -- so
automatic and manual selection stay one underlying code path, and this module
never re-implements selection validation (quantity caps, part resolution).

Allocation rules per line:
- Own Stock first: own-stock offers (a vendor named "Bijvasan" -- see
  `core.services.own_stock` -- or one with `Vendor.is_own_stock` set) are
  filled greedily before anyone else.
- The remaining quantity is then filled by the `combination` strategy, which
  draws from the best-stocked external vendor first, then the next, splitting
  across as many vendors as it takes. Drawing from the largest first means a
  single vendor is used when one can cover the line, and otherwise the
  practical minimum number of vendors is combined, higher-availability first.
- All-or-nothing: if the TOTAL available across all matching vendors is less
  than the requested quantity, NO allocation is made for that line (it is left
  unselected and reported as "Cannot Fulfill") -- the system never creates a
  partial allocation.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy.orm import Session

from core.models import VendorSelection
from core.services import customer_order_service, vendor_selection_service
from core.services.rules.registry import get_strategy
from core.services.vendor_comparison_service import (
    VendorComparisonRow,
    compare_vendors_for_order,
)


def _allocate_own_stock_first(
    order_item_id: int, requested_quantity: Decimal, offers: list[VendorComparisonRow], session: Session
) -> tuple[Decimal, list[VendorComparisonRow]]:
    """Greedily fills as much of `requested_quantity` as possible from
    own-stock offers, applying each allocation immediately. Returns the
    remaining quantity still needed (0 if own stock covered it all) and the
    external (non-own-stock) offers for the caller to hand to the chosen
    strategy."""
    own_stock_offers = [offer for offer in offers if offer.is_own_stock]
    other_offers = [offer for offer in offers if not offer.is_own_stock]

    remaining = requested_quantity
    for offer in sorted(
        own_stock_offers, key=lambda o: o.vendor_available_quantity or Decimal(0), reverse=True
    ):
        if remaining <= 0:
            break
        if offer.vendor_id is None or not offer.vendor_available_quantity:
            continue

        take = min(remaining, offer.vendor_available_quantity)
        vendor_selection_service.upsert_selection(order_item_id, offer.vendor_id, take, session)
        remaining -= take

    return remaining, other_offers


# The sole automatic strategy. The Founder no longer chooses one -- the
# system always combines vendors as needed (single vendor when one suffices).
_AUTOMATIC_STRATEGY = "combination"


def run_automatic_vendor_selection(order_id: int, session: Session) -> list[VendorSelection]:
    if customer_order_service.get_customer_order(order_id, session) is None:
        raise LookupError(f"Customer order {order_id} not found.")

    strategy = get_strategy(_AUTOMATIC_STRATEGY)
    comparison = compare_vendors_for_order(order_id, session)

    offers_by_item: dict[int, list[VendorComparisonRow]] = defaultdict(list)
    requested_by_item: dict[int, Decimal] = {}
    for row in comparison.rows:
        if row.order_item_id is None:
            continue
        requested_by_item[row.order_item_id] = row.requested_quantity
        if row.vendor_id is not None and row.vendor_available_quantity:
            offers_by_item[row.order_item_id].append(row)

    applied: list[VendorSelection] = []
    for order_item_id, requested_quantity in requested_by_item.items():
        offers = offers_by_item.get(order_item_id, [])

        # Always start from a clean slate for this line (drop any prior manual
        # or automatic picks) so a re-run never leaves stale vendors behind.
        vendor_selection_service.clear_selections_for_item(order_item_id, session)

        if not offers:
            continue

        # All-or-nothing: if every matching vendor together still can't cover
        # the requested quantity, make NO allocation -- the line stays
        # unselected and is reported as "Cannot Fulfill". No partial fulfilment.
        total_available = sum(
            (offer.vendor_available_quantity or Decimal(0) for offer in offers), Decimal(0)
        )
        if total_available < requested_quantity:
            continue

        remaining_quantity, external_offers = _allocate_own_stock_first(
            order_item_id, requested_quantity, offers, session
        )

        if remaining_quantity > 0 and external_offers:
            for allocation in strategy.allocate(remaining_quantity, external_offers):
                if allocation.quantity <= 0:
                    continue
                vendor_selection_service.upsert_selection(
                    order_item_id, allocation.vendor_id, allocation.quantity, session
                )

        applied.extend(vendor_selection_service.list_selections_for_item(order_item_id, session))

    return applied
