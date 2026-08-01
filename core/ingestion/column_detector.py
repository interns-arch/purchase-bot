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
