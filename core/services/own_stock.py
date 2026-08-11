"""Own Stock identification for Automatic Vendor Selection.

A vendor represents the company's OWN stock (its own warehouse / dark-store)
when EITHER:

  1. its `Vendor.is_own_stock` flag is set in the database (the original
     mechanism, kept working untouched), OR
  2. its name matches ANY of the configured own-stock vendor names -- read
     from the `OWN_STOCK_VENDOR_NAME` environment variable as a
     COMMA-SEPARATED list, default ``"Bijvasan, Bijwasan, Mansarovar,
     Mansarover"`` (both warehouses, every spelling variant seen in use).

Rule (2) means the company's own warehouse "vendors" are recognised
automatically the moment their inventory is uploaded under those names -- no
manual DB flag, and no UI, required. Whichever rule matches, the rule engine
(`core.services.rules.engine`) always allocates own-stock offers FIRST,
regardless of the chosen strategy (several own-stock vendors are drawn from
in order of available stock, biggest first); only the remaining requested
quantity is handed to the strategy against the external vendors.

The env var is read directly here (rather than via `backend.app.core.config`)
so `core/services/*` stays importable standalone -- the same pattern
`core.services.rules.registry` uses for VENDOR_SELECTION_DEFAULT_STRATEGY.

Name matching is deliberately tolerant so a slight rename doesn't silently
disable own-stock priority: each configured name is case-insensitive,
whitespace-tolerant, and matched as a WHOLE WORD anywhere in the vendor
name. So with `OWN_STOCK_VENDOR_NAME=Bijwasan,Mansarovar`, all of these are
own stock::

    "Bijwasan"            "BIJWASAN HUB"       "bijwasan"
    "Mansarovar"          "Mansarovar Depot"   "Main Bijwasan Warehouse"

while an unrelated name that merely happens to contain the letters (e.g.
"Bijwasannual Traders") is NOT matched -- the word boundary prevents that
false positive.
"""

from __future__ import annotations

import os
import re

# Both warehouses, with every spelling variant seen in real use (the
# production vendor is registered as "BIJVASAN"; the Founder also writes
# "Bijwasan" / "Mansarover") -- whole-word matching keeps this safe.
DEFAULT_OWN_STOCK_VENDOR_NAME = "Bijvasan, Bijwasan, Mansarovar, Mansarover"


def own_stock_vendor_names() -> list[str]:
    """The configured own-stock vendor names -- `OWN_STOCK_VENDOR_NAME`
    split on commas, blanks dropped (default: both warehouses in every
    spelling variant -- see `DEFAULT_OWN_STOCK_VENDOR_NAME`).

    An empty/blank `OWN_STOCK_VENDOR_NAME` disables name-based detection
    entirely (only the `is_own_stock` DB flag then marks own stock)."""
    raw = os.environ.get("OWN_STOCK_VENDOR_NAME", DEFAULT_OWN_STOCK_VENDOR_NAME)
    return [name.strip() for name in raw.split(",") if name.strip()]


def own_stock_vendor_name() -> str:
    """Backward-compatible single-value view of the configuration (the raw
    comma-separated string, stripped). Prefer `own_stock_vendor_names()`."""
    return os.environ.get("OWN_STOCK_VENDOR_NAME", DEFAULT_OWN_STOCK_VENDOR_NAME).strip()


def is_own_stock_vendor(vendor_name: str | None, *, flag: bool = False) -> bool:
    """Whether a vendor is the company's own stock.

    `flag` is that vendor's `Vendor.is_own_stock` column -- when True the
    vendor is own stock regardless of its name. Otherwise each configured
    own-stock name is matched (case-insensitively) as a whole word anywhere
    in `vendor_name` -- see the module docstring for exactly what matches."""
    if flag:
        return True

    configured_names = own_stock_vendor_names()
    if not configured_names or not vendor_name:
        return False

    name = vendor_name.strip().casefold()
    for configured in configured_names:
        target = configured.casefold()
        if name == target:
            return True
        # Whole-word match: the configured name bounded by a non-alphanumeric
        # character (or string start/end) on each side. Matches "Bijwasan
        # Warehouse" / "Main Bijwasan Depot" but not "Bijwasannual".
        pattern = rf"(?<![0-9a-z]){re.escape(target)}(?![0-9a-z])"
        if re.search(pattern, name) is not None:
            return True
    return False
