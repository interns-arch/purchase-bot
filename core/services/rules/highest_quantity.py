"""Highest Available Quantity strategy: always pick the single vendor with
the most stock on hand, capped at the requested quantity. Never splits a
line across vendors -- if that one vendor can't cover the full request, the
shortfall is simply left unallocated (same as this app's original
single-vendor-per-line matching philosophy)."""

from __future__ import annotations

from decimal import Decimal

from core.services.rules.base import VendorAllocation, VendorSelectionStrategy
from core.services.vendor_comparison_service import VendorComparisonRow


class HighestAvailableQuantityStrategy(VendorSelectionStrategy):
    name = "highest_quantity"

    def allocate(
        self, requested_quantity: Decimal, offers: list[VendorComparisonRow]
    ) -> list[VendorAllocation]:
        candidates = [
            offer
            for offer in offers
            if offer.vendor_id is not None and offer.vendor_available_quantity
        ]
        if not candidates:
            return []

        best = max(candidates, key=lambda o: o.vendor_available_quantity)
        quantity = min(requested_quantity, best.vendor_available_quantity)
        return [VendorAllocation(vendor_id=best.vendor_id, quantity=quantity)]
