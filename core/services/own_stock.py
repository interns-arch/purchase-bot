"""Own Stock identification for Automatic Vendor Selection.

A vendor represents the company's OWN stock (its own warehouse / dark-store)
when EITHER:

  1. its `Vendor.is_own_stock` flag is set in the database (the original
     mechanism, kept working untouched), OR
  2. its name matches the configured own-stock vendor name -- read from the
     `OWN_STOCK_VENDOR_NAME` environment variable, default ``"Bijvasan"``.

Rule (2) means the company's own warehouse "vendor" is recognised
automatically the moment its inventory is uploaded under that name -- no
manual DB flag, and no UI, required. Whichever rule matches, the rule engine
(`core.services.rules.engine`) always allocates own-stock offers FIRST,
regardless of the chosen strategy; only the remaining requested quantity is
handed to the strategy against the external (non-own-stock) vendors.

The env var is read directly here (rather than via `backend.app.core.config`)
so `core/services/*` stays importable standalone -- the same pattern
`core.services.rules.registry` uses for VENDOR_SELECTION_DEFAULT_STRATEGY.

Name matching is deliberately tolerant so a slight rename doesn't silently
disable own-stock priority: it's case-insensitive, whitespace-tolerant, and
matches the configured name as a WHOLE WORD anywhere in the vendor name. So
with `OWN_STOCK_VENDOR_NAME=Bijvasan`, all of these are own stock::

    "Bijvasan"            "BIJVASAN"           "bijvasan"
    "Bijvasan Warehouse"  "Bijvasan Hub"       "Main Bijvasan Depot"

while an unrelated name that merely happens to contain the letters (e.g.
"Bijvasannual Traders") is NOT matched -- the word boundary prevents that
false positive.
"""

from __future__ import annotations

import os
import re

DEFAULT_OWN_STOCK_VENDOR_NAME = "Bijvasan"


def own_stock_vendor_name() -> str:
    """The configured own-stock vendor name (default ``"Bijvasan"``).

    An empty/blank `OWN_STOCK_VENDOR_NAME` disables name-based detection
    entirely (only the `is_own_stock` DB flag then marks own stock)."""
    return os.environ.get("OWN_STOCK_VENDOR_NAME", DEFAULT_OWN_STOCK_VENDOR_NAME).strip()


def is_own_stock_vendor(vendor_name: str | None, *, flag: bool = False) -> bool:
    """Whether a vendor is the company's own stock.

    `flag` is that vendor's `Vendor.is_own_stock` column -- when True the
    vendor is own stock regardless of its name. Otherwise the configured
    own-stock name is matched (case-insensitively) as a whole word anywhere
    in `vendor_name` -- see the module docstring for exactly what matches."""
    if flag:
        return True

    configured = own_stock_vendor_name()
    if not configured or not vendor_name:
        return False

    name = vendor_name.strip().casefold()
    target = configured.casefold()
    if name == target:
        return True

    # Whole-word match: the configured name bounded by a non-alphanumeric
    # character (or string start/end) on each side. Matches "Bijvasan
    # Warehouse" / "Main Bijvasan Depot" but not "Bijvasannual".
    pattern = rf"(?<![0-9a-z]){re.escape(target)}(?![0-9a-z])"
    return re.search(pattern, name) is not None
