"""Extension point for Automatic Vendor Selection strategies.

Every strategy takes the requested quantity for one order line plus every
vendor offer the Vendor Comparison report found for that line (already
sorted best-stocked-first by `vendor_comparison_service.compare_vendors`),
and returns the list of (vendor, quantity) allocations to apply.

Concrete strategies never touch the database or call
`vendor_selection_service` themselves -- that stays in `engine.py`, so a
future AI-based strategy only has to implement `allocate()` below; nothing
else in the selection pipeline needs to change to support it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from core.services.vendor_comparison_service import VendorComparisonRow


@dataclass
class VendorAllocation:
    vendor_id: int
    quantity: Decimal


class VendorSelectionStrategy(ABC):
    """Interface a rule (or future AI-based) vendor-selection strategy must
    implement."""

    name: str

    @abstractmethod
    def allocate(
        self, requested_quantity: Decimal, offers: list[VendorComparisonRow]
    ) -> list[VendorAllocation]:
        """`offers` only ever contains rows for ONE order line, each with a
        resolved `vendor_id` and a positive `vendor_available_quantity`.
        Must never return an allocation whose quantity exceeds that offer's
        `vendor_available_quantity`, and the allocations' quantities must
        never sum to more than `requested_quantity`."""
        raise NotImplementedError
