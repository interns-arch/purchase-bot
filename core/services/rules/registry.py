"""Name -> strategy lookup for Automatic Vendor Selection. Adding a new
strategy (including a future AI-based one) only requires implementing
`VendorSelectionStrategy` and registering it here -- nothing else in the
selection pipeline changes."""

from __future__ import annotations

import os

from core.services.rules.base import VendorSelectionStrategy
from core.services.rules.combination import CombinationStrategy
from core.services.rules.highest_quantity import HighestAvailableQuantityStrategy
from core.services.rules.minimum_vendors import MinimumVendorCountStrategy

_STRATEGIES: dict[str, VendorSelectionStrategy] = {
    HighestAvailableQuantityStrategy.name: HighestAvailableQuantityStrategy(),
    MinimumVendorCountStrategy.name: MinimumVendorCountStrategy(),
    CombinationStrategy.name: CombinationStrategy(),
}

STRATEGY_NAMES = tuple(_STRATEGIES.keys())

# The default strategy when the caller (API request or a future scheduled
# job) doesn't specify one. Configurable via env so ops can change the
# default without a code change or redeploy of calling code.
DEFAULT_STRATEGY_NAME = os.environ.get("VENDOR_SELECTION_DEFAULT_STRATEGY", "combination")
if DEFAULT_STRATEGY_NAME not in _STRATEGIES:
    DEFAULT_STRATEGY_NAME = "combination"


def get_strategy(name: str) -> VendorSelectionStrategy:
    try:
        return _STRATEGIES[name]
    except KeyError:
        raise ValueError(
            f"Unknown vendor selection strategy '{name}'. Choose one of: "
            f"{', '.join(STRATEGY_NAMES)}."
        ) from None
