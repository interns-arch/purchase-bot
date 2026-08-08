"""Combination-of-vendors strategy: satisfy the requested quantity by
greedily drawing from the best-stocked vendor first, then the next, and so
on, until the requested quantity is met or every offer is exhausted --
splitting a single order line across as many vendors as it actually takes.

"Best-stocked" is ranked by each vendor's RAW imported capacity
(`vendor_raw_available_quantity`), a stable ordering that doesn't reshuffle
as other customers' orders consume stock -- but the actual AMOUNT drawn from
each vendor is always its REMAINING quantity (`vendor_available_quantity`,
raw minus every other customer order's reservation), so a vendor already
partly or fully claimed by another order simply contributes less (or
nothing) without displacing its position in the draw order."""

from __future__ import annotations

from decimal import Decimal

from core.services.rules.base import VendorAllocation, VendorSelectionStrategy
from core.services.vendor_comparison_service import VendorComparisonRow


class CombinationStrategy(VendorSelectionStrategy):
    name = "combination"

    def allocate(
        self, requested_quantity: Decimal, offers: list[VendorComparisonRow]
    ) -> list[VendorAllocation]:
        remaining = requested_quantity
        allocations: list[VendorAllocation] = []

        for offer in sorted(
            offers, key=lambda o: o.vendor_raw_available_quantity or Decimal(0), reverse=True
        ):
            if remaining <= 0:
                break
            if offer.vendor_id is None or not offer.vendor_available_quantity:
                continue

            take = min(remaining, offer.vendor_available_quantity)
            allocations.append(VendorAllocation(vendor_id=offer.vendor_id, quantity=take))
            remaining -= take

        return allocations
