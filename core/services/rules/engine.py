"""Automatic Vendor Selection: runs the Vendor Comparison report for a
customer order through a chosen strategy, then applies the resulting
allocations via the SAME `vendor_selection_service.upsert_selection` manual
selection already uses -- so automatic and manual selection are always one
underlying code path, and this module never needs to re-implement selection
validation (quantity caps, part resolution, etc.).

Own Stock always gets first priority, regardless of which strategy is
chosen: each line's own-stock offers (any vendor named "Bijvasan" -- see
`core.services.own_stock` -- or one with its `Vendor.is_own_stock` flag set,
i.e. the company's own warehouse/dark-store) are filled greedily first, and
only the REMAINING requested quantity (if any) is handed to the chosen
strategy, which then only ever sees the external (non-own-stock) offers. This
is a pre-step in front of every strategy, not a strategy of its own.
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


def run_automatic_vendor_selection(
    order_id: int, strategy_name: str, session: Session
) -> list[VendorSelection]:
    if customer_order_service.get_customer_order(order_id, session) is None:
        raise LookupError(f"Customer order {order_id} not found.")

    strategy = get_strategy(strategy_name)
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
        if not offers:
            continue

        # Replace whatever was previously selected for this line (manual or
        # a prior automatic run) so switching strategies -- or re-running
        # the same one -- never leaves stale vendors from an earlier
        # allocation sitting alongside the new ones.
        vendor_selection_service.clear_selections_for_item(order_item_id, session)

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
