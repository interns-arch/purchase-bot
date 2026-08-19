"""Inventory Search: search every vendor's active inventory for each
customer order line and report EVERY vendor that stocks the part.

This module deliberately does NOT choose a vendor -- see the workflow note
in order_matching.py's module docstring. Vendor Selection (manual or
rule-based: lowest MRP, lowest sale price, highest available quantity,
vendor performance score, or a combination) is a separate, not-yet-built
module that will consume this report later. Because this module only
searches and lists, adding that future selection logic will never require
changes here.

Pure business logic -- no print()/input() here; see order_matching.py for
the CLI layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import openpyxl
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import PartAlias

from core.ingestion.column_detector import (
    DISCOUNT_HEADERS,
    DESCRIPTION_HEADERS,
    MRP_HEADERS,
    PRICE_HEADERS,
    decimal_to_string,
    is_parseable_quantity,
    normalise_header,
    normalise_part_number,
    parse_quantity,
)
from core.services.customer_order_service import list_customer_order_items
from core.services.inventory_import_service import (
    MasterInventoryVendorEntry,
    get_master_inventory,
)
from core.services import vendor_stock_service

AVAILABLE = "Available"
PARTIAL = "Partial"
OUT_OF_STOCK = "Out of Stock"
NOT_FOUND = "Not Found"


def _raw_text(raw_data: dict | None, headers: set[str]) -> str | None:
    for key, value in (raw_data or {}).items():
        if normalise_header(key) in headers:
            text = str(value).strip()
            if text:
                return text
    return None


def _raw_decimal(raw_data: dict | None, headers: set[str]) -> Decimal | None:
    text = _raw_text(raw_data, headers)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _resolve_mrp(offer: MasterInventoryVendorEntry) -> Decimal | None:
    if offer.mrp is not None:
        return offer.mrp
    return _raw_decimal(offer.raw_data, MRP_HEADERS)


def _resolve_sale_price(offer: MasterInventoryVendorEntry) -> Decimal | None:
    if offer.price is not None:
        return offer.price
    return _raw_decimal(offer.raw_data, PRICE_HEADERS)


def _resolve_discount(offer: MasterInventoryVendorEntry) -> str | None:
    return _raw_text(offer.raw_data, DISCOUNT_HEADERS)


def _resolve_description(
    part_description: str | None, offer: MasterInventoryVendorEntry
) -> str | None:
    if part_description:
        return part_description
    return _raw_text(offer.raw_data, DESCRIPTION_HEADERS)


def _stock_status(available_quantity: Decimal, requested_quantity: Decimal) -> str:
    if available_quantity <= 0:
        return OUT_OF_STOCK
    if available_quantity >= requested_quantity:
        return AVAILABLE
    return PARTIAL


@dataclass
class VendorComparisonRow:
    customer_part_number: str
    requested_quantity: Decimal
    vendor_name: str | None
    vendor_part_number: str | None
    part_description: str | None
    brand: str | None
    # REMAINING quantity: raw imported stock minus every OTHER customer
    # order's reservation (see `vendor_stock_service.remaining_quantity`) --
    # this is the number that's actually safe to allocate or display.
    vendor_available_quantity: Decimal | None
    mrp: Decimal | None
    sale_price: Decimal | None
    discount: str | None
    stock_status: str
    inventory_file: str | None
    order_item_id: int | None = None
    vendor_id: int | None = None
    is_own_stock: bool = False
    # RAW imported quantity (never reduced by other orders' reservations) --
    # used only so the automatic engine's greedy "biggest vendor first"
    # ordering (`combination.py`, `_allocate_own_stock_first`) stays a stable
    # ranking by real capacity instead of reshuffling every time another
    # customer's order consumes some of a vendor's remaining stock.
    vendor_raw_available_quantity: Decimal | None = None


@dataclass
class VendorComparisonSummary:
    customer_order_items: int = 0
    matched_items: int = 0
    not_found_items: int = 0
    invalid_items: int = 0
    matching_vendors_found: int = 0


@dataclass
class VendorComparisonResult:
    rows: list[VendorComparisonRow] = field(default_factory=list)
    summary: VendorComparisonSummary = field(default_factory=VendorComparisonSummary)


def _not_found_row(
    part_number: str, requested_quantity: Decimal, *, order_item_id: int | None = None
) -> VendorComparisonRow:
    return VendorComparisonRow(
        customer_part_number=part_number,
        requested_quantity=requested_quantity,
        vendor_name=None,
        vendor_part_number=None,
        part_description=None,
        brand=None,
        vendor_available_quantity=None,
        mrp=None,
        sale_price=None,
        discount=None,
        stock_status=NOT_FOUND,
        inventory_file=None,
        order_item_id=order_item_id,
    )


def compare_vendors(
    input_rows: list[dict[str, str]],
    part_column: str,
    quantity_column: str,
    session: Session,
    *,
    id_column: str | None = None,
) -> VendorComparisonResult:
    """Search every active vendor inventory for every customer order line.

    Lists ALL vendors that stock a part -- never picks one. Rows for a given
    part are sorted with the best-stocked vendor first so the Purchase Team
    can compare at a glance.

    `vendor_available_quantity` is each vendor's REMAINING quantity --
    raw imported stock minus whatever is already reserved by every OTHER
    customer order's `VendorSelection` for that same vendor+part (see
    `vendor_stock_service.remaining_quantity`), clamped at 0 for display.
    This is the single choke point Automatic Vendor Selection
    (`rules/engine.py`) and every export/report read from, so fixing it here
    means neither of those needs its own separate double-allocation check to
    stay correct.

    `id_column`, when given, names a column in `input_rows` holding the
    originating `CustomerOrderItem.id` (as a string) -- passed through as
    `VendorComparisonRow.order_item_id` so a later Vendor Selection step can
    tie a chosen offer back to the exact order line, and so THIS row's own
    existing reservation (if any) is excluded from its own "already
    reserved" figure. CLI callers (which have no such row identity) simply
    omit it and get `order_item_id=None` -- their remaining-quantity figure
    then reflects every reservation in the system, since there's no "this
    line's own row" to exclude.
    """
    master_rows = get_master_inventory(session)
    offer_index = {part_row.canonical_part_number: part_row for part_row in master_rows}
    # ALIAS-AWARE matching: a part ordered under ANY of its known numbers --
    # a vendor's alternate column ("Root Part Num" / "OEM Part No"), or a
    # different vendor's spelling -- must find the same offers. Every
    # PartAlias of an in-stock part is added as an extra lookup key;
    # `setdefault` keeps canonical numbers authoritative on collision.
    by_part_id = {part_row.part_id: part_row for part_row in master_rows}
    if by_part_id:
        for alias_norm, alias_part_id in session.execute(
            select(PartAlias.normalized_part_number, PartAlias.part_id).where(
                PartAlias.part_id.in_(list(by_part_id))
            )
        ):
            offer_index.setdefault(alias_norm, by_part_id[alias_part_id])

    # FOUNDER-DECLARED equivalences last (`part_link_service`): numbers a
    # human has stated are the same part ("MF390300ML32" / "MF390300ML")
    # become extra keys onto the offers their partner already found. Added
    # after aliases so a real canonical/alias match always wins.
    from core.services import part_link_service

    part_link_service.apply_links_to_index(offer_index, session)

    result = VendorComparisonResult()
    result.summary.customer_order_items = len(input_rows)

    for row in input_rows:
        raw_part_number = row.get(part_column, "").strip()
        raw_quantity = row.get(quantity_column, "")
        raw_order_item_id = row.get(id_column) if id_column else None
        order_item_id = int(raw_order_item_id) if raw_order_item_id else None

        if (
            not raw_part_number
            or not is_parseable_quantity(raw_quantity)
            or parse_quantity(raw_quantity) <= 0
        ):
            result.summary.invalid_items += 1
            continue

        requested_quantity = parse_quantity(raw_quantity)
        normalized = normalise_part_number(raw_part_number)
        part_row = offer_index.get(normalized)

        if part_row is None or not part_row.vendors:
            result.rows.append(
                _not_found_row(
                    raw_part_number, requested_quantity, order_item_id=order_item_id
                )
            )
            result.summary.not_found_items += 1
            continue

        result.summary.matched_items += 1

        # The AMOUNT reported/allocated per vendor is remaining (raw imported
        # minus what every OTHER customer order already reserved) -- this is
        # what closes the double-allocation gap. The SORT ORDER stays keyed
        # on raw imported quantity, unchanged from before this fix: which
        # vendor counts as "best-stocked" is the vendor's real total
        # capacity, a stable ranking that doesn't reshuffle line-to-line as
        # other customers' orders consume stock. A vendor whose remaining
        # has been drawn down to 0 still sorts in its usual place but
        # contributes nothing (`combination.py` already skips any offer with
        # no remaining quantity), so this never causes a fully-consumed
        # vendor to be picked over one that still has stock.
        remaining_by_vendor: dict[int, Decimal] = {
            offer.vendor_id: max(
                vendor_stock_service.remaining_quantity(
                    offer.vendor_id,
                    part_row.part_id,
                    offer.quantity_available,
                    session,
                    exclude_order_item_id=order_item_id,
                ),
                Decimal(0),
            )
            for offer in part_row.vendors
        }

        ordered_offers = sorted(
            part_row.vendors,
            key=lambda offer: (-offer.quantity_available, offer.vendor_name.lower()),
        )

        for offer in ordered_offers:
            remaining = remaining_by_vendor[offer.vendor_id]
            result.rows.append(
                VendorComparisonRow(
                    customer_part_number=raw_part_number,
                    requested_quantity=requested_quantity,
                    vendor_name=offer.vendor_name,
                    vendor_id=offer.vendor_id,
                    vendor_part_number=offer.vendor_part_number,
                    part_description=_resolve_description(
                        part_row.part_description, offer
                    ),
                    brand=part_row.brand,
                    vendor_available_quantity=remaining,
                    vendor_raw_available_quantity=offer.quantity_available,
                    mrp=_resolve_mrp(offer),
                    sale_price=_resolve_sale_price(offer),
                    discount=_resolve_discount(offer),
                    stock_status=_stock_status(remaining, requested_quantity),
                    inventory_file=offer.inventory_file,
                    order_item_id=order_item_id,
                    is_own_stock=offer.is_own_stock,
                )
            )
            result.summary.matching_vendors_found += 1

    return result


def compare_vendors_for_order(order_id: int, session: Session) -> VendorComparisonResult:
    """Same matching engine as `compare_vendors`, but reading a persisted
    `CustomerOrder` (from the web Customer Orders module) instead of a raw
    file's rows -- the web app's equivalent of `order_matching.py` reading
    `input.csv`. Column detection doesn't apply here since the order was
    already validated and normalized at import time (see
    `customer_order_service.run_customer_order_import`), so the two
    synthetic column names below just need to agree with each other."""
    part_column = "Part Number"
    quantity_column = "Quantity"
    id_column = "__order_item_id__"

    input_rows = [
        {
            part_column: item.part_number_raw,
            quantity_column: decimal_to_string(item.quantity_requested),
            id_column: str(item.id),
        }
        for item in list_customer_order_items(order_id, session)
    ]

    return compare_vendors(
        input_rows, part_column, quantity_column, session, id_column=id_column
    )


REPORT_HEADERS = [
    "Customer Part Number",
    "Requested Qty",
    "Vendor Name",
    "Vendor Part Number",
    "Part Description",
    "Brand",
    "Vendor Available Qty",
    "MRP",
    "Sale Price",
    "Discount",
    "Stock Status",
    "Inventory File",
]


def _decimal_cell(value: Decimal | None) -> str:
    return decimal_to_string(value) if value is not None else ""


def _report_row_cells(row: VendorComparisonRow) -> list[str]:
    return [
        row.customer_part_number,
        decimal_to_string(row.requested_quantity),
        row.vendor_name or "-",
        row.vendor_part_number or "-",
        row.part_description or "",
        row.brand or "",
        _decimal_cell(row.vendor_available_quantity),
        _decimal_cell(row.mrp),
        _decimal_cell(row.sale_price),
        row.discount or "",
        row.stock_status,
        row.inventory_file or "-",
    ]


def to_workbook(rows: list[VendorComparisonRow]) -> openpyxl.Workbook:
    """Build the Vendor Comparison Report workbook -- the exact same shape
    `order_matching.py` has always exported (same headers, same column
    order), so the CLI (writes to `output/vendor_comparison_report.xlsx`)
    and the web app's "Export to Excel" button never drift apart."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Vendor Comparison"

    sheet.append(REPORT_HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in rows:
        sheet.append(_report_row_cells(row))

    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value)) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = max_length + 2

    return workbook
