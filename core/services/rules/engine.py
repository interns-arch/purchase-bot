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
- Partial fulfilment allowed: if the TOTAL available across all matching
  vendors is less than the requested quantity, the line is still allocated
  as much as the available vendors can supply (never more than requested),
  and the shortfall is reported as PARTIALLY FULFILLED. A line is left
  completely unselected only when no vendor has any available quantity.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from core.logging_setup import get_logger
from core.models import VendorSelection
from core.services import customer_order_service, vendor_selection_service
from core.services.rules.registry import get_strategy
from core.services.vendor_comparison_service import (
    VendorComparisonRow,
    compare_vendors_for_order,
)

logger = get_logger(__name__)


def _try_upsert_selection(
    order_item_id: int, vendor_id: int, quantity: Decimal, session: Session
) -> bool:
    """Same call `vendor_selection_service.upsert_selection` always uses to
    apply an allocation -- but the AUTOMATIC engine treats a rejection (this
    vendor turned out to have less remaining than the snapshot this run
    started from suggested -- e.g. an earlier line in this same order, or a
    concurrent order, just claimed some of it) as "skip this vendor for this
    line" rather than a fatal error that aborts the rest of the order's
    automatic selection. Manual selection (the API endpoint) still lets the
    same `ValueError` surface directly to its caller -- only the automatic
    engine gets this graceful treatment, since it has no user watching a
    single request to react to a rejection. Returns True if applied."""
    try:
        vendor_selection_service.upsert_selection(order_item_id, vendor_id, quantity, session)
        return True
    except ValueError:
        logger.warning(
            "Automatic Vendor Selection: vendor %s no longer had %s remaining for "
            "order item %s (claimed elsewhere since this run started) -- skipping.",
            vendor_id,
            quantity,
            order_item_id,
        )
        return False


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
        own_stock_offers, key=lambda o: o.vendor_raw_available_quantity or Decimal(0), reverse=True
    ):
        if remaining <= 0:
            break
        if offer.vendor_id is None or not offer.vendor_available_quantity:
            continue

        take = min(remaining, offer.vendor_available_quantity)
        if _try_upsert_selection(order_item_id, offer.vendor_id, take, session):
            remaining -= take

    return remaining, other_offers


# The sole automatic strategy. The Founder no longer chooses one -- the
# system always combines vendors as needed (single vendor when one suffices).
_AUTOMATIC_STRATEGY = "combination"


def run_automatic_vendor_selection(order_id: int, session: Session) -> list[VendorSelection]:
    if customer_order_service.get_customer_order(order_id, session) is None:
        raise LookupError(f"Customer order {order_id} not found.")

    strategy = get_strategy(_AUTOMATIC_STRATEGY)

    # Which lines exist and how much each requests comes from the customer's
    # own order -- that doesn't change during this run, so one read is fine.
    # Vendor AVAILABILITY, in contrast, is re-read fresh per line below.
    requested_by_item: dict[int, Decimal] = {}
    for row in compare_vendors_for_order(order_id, session).rows:
        if row.order_item_id is not None:
            requested_by_item[row.order_item_id] = row.requested_quantity

    applied: list[VendorSelection] = []
    for order_item_id, requested_quantity in requested_by_item.items():
        # Always start from a clean slate for this line (drop any prior manual
        # or automatic picks) so a re-run never leaves stale vendors behind.
        vendor_selection_service.clear_selections_for_item(order_item_id, session)

        # Re-read vendor availability fresh for EVERY line, right before
        # deciding its allocation -- not from a snapshot taken once for the
        # whole order. Otherwise an earlier line in this SAME order (or a
        # concurrent order committing in between) that already consumed some
        # of a vendor's remaining stock would be invisible to this line's
        # decision. Cheap at this scale -- one extra query per line.
        offers: list[VendorComparisonRow] = [
            row
            for row in compare_vendors_for_order(order_id, session).rows
            if row.order_item_id == order_item_id
            and row.vendor_id is not None
            and row.vendor_available_quantity
        ]

        if not offers:
            continue

        # Note: no all-or-nothing guard here -- a line whose matching vendors
        # together can't fully cover the requested quantity is still PARTIALLY
        # fulfilled (allocate as much as available, never more than requested).
        # The `combination` strategy below already stops drawing once every
        # offer is exhausted, so the shortfall is simply reported as
        # PARTIALLY FULFILLED instead of discarding all allocations.

        remaining_quantity, external_offers = _allocate_own_stock_first(
            order_item_id, requested_quantity, offers, session
        )

        if remaining_quantity > 0 and external_offers:
            for allocation in strategy.allocate(remaining_quantity, external_offers):
                if allocation.quantity <= 0:
                    continue
                _try_upsert_selection(
                    order_item_id, allocation.vendor_id, allocation.quantity, session
                )

        applied.extend(vendor_selection_service.list_selections_for_item(order_item_id, session))

    return applied
