"""Minimum Number of Vendors strategy: prefer exactly one vendor whenever
possible -- same "smallest sufficient offer wins" rule the original CLI
matching engine used (leaves larger stocks free for other orders). Only
when no single vendor can cover the full requested quantity does it fall
back to splitting across the fewest vendors it can (delegated to
`CombinationStrategy`, which already greedily drains the best-stocked
vendors first)."""

from __future__ import annotations

from decimal import Decimal

from core.services.rules.base import VendorAllocation, VendorSelectionStrategy
from core.services.rules.combination import CombinationStrategy
from core.services.vendor_comparison_service import VendorComparisonRow


class MinimumVendorCountStrategy(VendorSelectionStrategy):
    name = "minimum_vendors"

    def __init__(self) -> None:
        self._fallback = CombinationStrategy()

    def allocate(
        self, requested_quantity: Decimal, offers: list[VendorComparisonRow]
    ) -> list[VendorAllocation]:
        sufficient = [
            offer
            for offer in offers
            if offer.vendor_id is not None
            and offer.vendor_available_quantity
            and offer.vendor_available_quantity >= requested_quantity
        ]
        if sufficient:
            smallest_sufficient = min(sufficient, key=lambda o: o.vendor_available_quantity)
            return [
                VendorAllocation(
                    vendor_id=smallest_sufficient.vendor_id, quantity=requested_quantity
                )
            ]

        return self._fallback.allocate(requested_quantity, offers)
