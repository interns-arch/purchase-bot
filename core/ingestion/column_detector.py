"""Column detection and value normalization.

Lifted verbatim (behavior-for-behavior) from the original ``ordermatching.py``
script so both the legacy order-matching script and the new inventory-import
pipeline share identical normalization rules.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

# The script searches for these possible header names.
PART_NUMBER_HEADERS = {
    "partno",
    "PartNo",
    "Part Num",
    "partnumber",
    "partnum",
    "partcode",
    "itemcode",
    "itemnumber",
    "sku",
    "productcode",
}

QUANTITY_HEADERS = {
    "quantity",
    "qty",
    "availablequantity",
    "availableqty",
    "stockquantity",
    "stockqty",
    "orderedquantity",
    "orderedqty",
}

# --- Vendor Inventory-specific header aliases -------------------------------
# Real-world vendor inventory files use many names for the same two required
# columns. These supersets are used ONLY by the Vendor Inventory importer
# (`find_inventory_columns`); the base PART_NUMBER_HEADERS / QUANTITY_HEADERS
# above are left untouched so Customer Order / Delivery detection is unchanged.

INVENTORY_PART_NUMBER_HEADERS = PART_NUMBER_HEADERS | {
    "partnumber",  # Part Number / Part_Number
    "partnum",     # Part Num
    "partno", 
    "Part Num", 
    "PartNo",    # PartNo / Part No
}

# Explicit, curated quantity aliases -- NEVER derived from arbitrary numeric
# columns. Deliberately EXCLUDES MRP, price, and "Float Stock" (floatstock):
# only these named columns count as available quantity.
INVENTORY_QUANTITY_HEADERS = {
    "quantity",
    "qty",
    "availablequantity",   # Available Quantity
    "availableqty",        # Available Qty
    "availablestock",      # Available Stock
    "stock",               # Stock
    "stockquantity",
    "stockqty",
    "currentstock",        # Current Stock
    "currentst",           # Current St (truncated display of Current Stock)
    "currentstockqty",     # Current Stock Qty
    "partquantity",        # "part Quantity" (e.g. DELHI.csv)
    "partqty",
    # Tally / Indian accounting exports (e.g. MAHINDRA.xlsx) call on-hand
    # stock "Closing Stock". These are unambiguous on-hand quantities -- note
    # "Closing Value"/"Closing Amount" are deliberately NOT here, since those
    # are money, and money must never become a quantity.
    "closingstock",        # Closing Stock
    "closingqty",          # Closing Qty
    "closingquantity",     # Closing Quantity
    "closingbalance",      # Closing Balance
    "closingstockqty",     # Closing Stock Qty
}
# NOTE: every entry above must already be in `normalise_header` form
# (lowercase, alphanumeric only). An entry like "Current Stock" can never
# match, because headers are normalised before lookup.


VENDOR_NAME_HEADERS = {
    "vendor",
    "vendorname",
    "supplier",
    "suppliername",
}

PO_NUMBER_HEADERS = {
    "ponumber",
    "pono",
    "ponum",
    "po",
    "purchaseorder",
    "purchaseordernumber",
}

DELIVERED_QUANTITY_HEADERS = {
    "deliveredquantity",
    "deliveredqty",
    "deliveredqnty",
    "qtydelivered",
    "deliveryqty",
    "deliveryquantity",
}

# Optional -- not every vendor delivery file includes a date; when absent,
# callers fall back to the import timestamp (see `VendorDeliveryItem`).
DELIVERY_DATE_HEADERS = {
    "deliverydate",
    "date",
    "shippeddate",
    "shipdate",
    "dispatchdate",
    "invoicedate",
}

# Optional columns: not every vendor file provides these, so callers treat a
# missing column as "no data" rather than raising (see find_required_columns
# for the required-column equivalent).
PRICE_HEADERS = {"price", "saleprice", "unitprice", "rate", "sellingprice"}
MRP_HEADERS = {"mrp", "maximumretailprice", "mrprice"}
DISCOUNT_HEADERS = {"discount", "discountpct", "discountpercent", "discountamount"}
DESCRIPTION_HEADERS = {
    "description",
    "partdescription",
    "itemdescription",
    "productdescription",
    "desc",
}


def normalise_header(value: str | None) -> str:
    """
    Convert headers such as 'Part No.', 'PART_NO' and 'part no'
    into the comparable value 'partno'.
    """
    if value is None:
        return ""

    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def normalise_part_number(value: Any) -> str:
    """
    Normalise a part number for matching.

    Matching is:
    - case-insensitive
    - unaffected by leading/trailing spaces
    - unaffected by spaces, hyphens, underscores and dots

    Examples:
        ABC-123  -> ABC123
        abc 123  -> ABC123
    """
    if value is None:
        return ""

    cleaned = str(value).strip().upper()
    return re.sub(r"[\s\-_.]+", "", cleaned)


def parse_quantity(value: Any) -> Decimal:
    """
    Convert a quantity value into Decimal.

    Supports values such as:
        10
        10.5
        1,000
        " 25 "

    Unparseable input floors to Decimal("0") -- callers that need to treat
    this as a loggable error (e.g. the inventory import service) must check
    for unparseable input *before* calling this function.
    """
    if value is None:
        return Decimal("0")

    text = str(value).strip()

    if not text:
        return Decimal("0")

    # Remove comma-based thousand separators.
    text = text.replace(",", "")

    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def is_parseable_quantity(value: Any) -> bool:
    """Return True if `value` would parse to a non-default Decimal quantity."""
    if value is None:
        return False

    text = str(value).strip().replace(",", "")

    if not text:
        return False

    try:
        Decimal(text)
    except InvalidOperation:
        return False

    return True


def decimal_to_string(value: Decimal) -> str:
    """
    Format Decimal without unnecessary trailing zeroes.
    """
    if value == value.to_integral_value():
        return str(int(value))

    return format(value.normalize(), "f")


def find_required_columns(
    headers: list[str],
    file_name: str,
) -> tuple[str, str]:
    """
    Find the part-number and quantity columns from a CSV/Excel header row.
    """
    header_lookup = {
        normalise_header(header): header
        for header in headers
        if header is not None
    }

    part_number_column = next(
        (
            original_header
            for normalised, original_header in header_lookup.items()
            if normalised in PART_NUMBER_HEADERS
        ),
        None,
    )

    quantity_column = next(
        (
            original_header
            for normalised, original_header in header_lookup.items()
            if normalised in QUANTITY_HEADERS
        ),
        None,
    )

    if not part_number_column:
        raise ValueError(
            f"Part-number column not found in '{file_name}'. "
            f"Headers found: {headers}"
        )

    if not quantity_column:
        raise ValueError(
            f"Quantity column not found in '{file_name}'. "
            f"Headers found: {headers}"
        )

    return part_number_column, quantity_column


def detect_header_row(
    grid: list[list[str]],
    required_headers: set[str] = PART_NUMBER_HEADERS,
    *,
    max_scan_rows: int = 100,
) -> int | None:
    """Locate the line-item header row in a raw grid that may have metadata
    rows above the table (multi-section files). Returns the 0-based index of
    the first row that both:

      - contains a header matching `required_headers` (default: a part-number
        header), and
      - has at least two non-empty cells (i.e. looks like a table header, not
        a single "Label: value" metadata line).

    For a simple file whose header is on row 0 this returns 0, so the ordinary
    single-section format keeps working. Returns None if no such row is found
    within `max_scan_rows` (the caller treats that as "part-number column not
    found")."""
    for index, row in enumerate(grid[:max_scan_rows]):
        normalised = [normalise_header(cell) for cell in row]
        non_empty = [value for value in normalised if value]
        if len(non_empty) >= 2 and any(value in required_headers for value in normalised):
            return index
    return None


def find_inventory_columns(headers: list[str], file_name: str) -> tuple[str, str]:
    """Find the part-number and available-quantity columns for a Vendor
    Inventory file, using the tolerant inventory alias sets. Quantity is
    matched ONLY against the curated `INVENTORY_QUANTITY_HEADERS` -- MRP,
    price, and Float Stock are never treated as quantity. Raises `ValueError`
    with a clear message if either required column is absent."""
    part_number_column = find_optional_column(headers, INVENTORY_PART_NUMBER_HEADERS)
    quantity_column = find_optional_column(headers, INVENTORY_QUANTITY_HEADERS)

    if not part_number_column:
        raise ValueError(
            f"Part-number column not found in '{file_name}'. Headers found: {headers}"
        )
    if not quantity_column:
        raise ValueError(
            f"Available-quantity column not found in '{file_name}' (looked for e.g. "
            f"Quantity / Available Qty / Current Stock / Stock). Headers found: {headers}"
        )

    return part_number_column, quantity_column


def find_optional_column(headers: list[str], candidate_headers: set[str]) -> str | None:
    """
    Find a column by normalized header name, returning None (never raising)
    when it isn't present -- for optional fields like price/MRP/description
    that not every vendor file provides.
    """
    header_lookup = {
        normalise_header(header): header
        for header in headers
        if header is not None
    }

    return next(
        (
            original_header
            for normalised, original_header in header_lookup.items()
            if normalised in candidate_headers
        ),
        None,
    )


def find_delivery_columns(
    headers: list[str],
    file_name: str,
) -> tuple[str, str, str, str]:
    """
    Find the vendor, PO-number, part-number and delivered-quantity columns
    from a delivery file's header row.

    Returns (vendor_column, po_number_column, part_number_column, delivered_quantity_column).
    """
    header_lookup = {
        normalise_header(header): header
        for header in headers
        if header is not None
    }

    def _find(candidate_headers: set[str]) -> str | None:
        return next(
            (
                original_header
                for normalised, original_header in header_lookup.items()
                if normalised in candidate_headers
            ),
            None,
        )

    vendor_column = _find(VENDOR_NAME_HEADERS)
    po_number_column = _find(PO_NUMBER_HEADERS)
    part_number_column = _find(PART_NUMBER_HEADERS)
    delivered_quantity_column = _find(DELIVERED_QUANTITY_HEADERS)

    missing = [
        label
        for label, column in (
            ("vendor", vendor_column),
            ("PO number", po_number_column),
            ("part number", part_number_column),
            ("delivered quantity", delivered_quantity_column),
        )
        if column is None
    ]

    if missing:
        raise ValueError(
            f"Column(s) {missing} not found in '{file_name}'. Headers found: {headers}"
        )

    return vendor_column, po_number_column, part_number_column, delivered_quantity_column


def find_vendor_delivery_columns(
    headers: list[str],
    file_name: str,
) -> tuple[str, str, str, str | None]:
    """
    Find the vendor, part-number, delivered-quantity, and (optional)
    delivery-date columns from a web-app vendor delivery file's header
    row -- no PO Number column, unlike `find_delivery_columns` above (the
    web app has no Purchase Order concept; deliveries are matched by
    vendor + part directly).

    Returns (vendor_column, part_number_column, delivered_quantity_column,
    delivery_date_column | None).
    """
    header_lookup = {
        normalise_header(header): header
        for header in headers
        if header is not None
    }

    def _find(candidate_headers: set[str]) -> str | None:
        return next(
            (
                original_header
                for normalised, original_header in header_lookup.items()
                if normalised in candidate_headers
            ),
            None,
        )

    vendor_column = _find(VENDOR_NAME_HEADERS)
    part_number_column = _find(PART_NUMBER_HEADERS)
    delivered_quantity_column = _find(DELIVERED_QUANTITY_HEADERS)
    delivery_date_column = _find(DELIVERY_DATE_HEADERS)

    missing = [
        label
        for label, column in (
            ("vendor", vendor_column),
            ("part number", part_number_column),
            ("delivered quantity", delivered_quantity_column),
        )
        if column is None
    ]

    if missing:
        raise ValueError(
            f"Column(s) {missing} not found in '{file_name}'. Headers found: {headers}"
        )

    return vendor_column, part_number_column, delivered_quantity_column, delivery_date_column
