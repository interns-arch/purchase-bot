"""Vendor Selection: lets the user pick exactly one vendor + quantity per
`CustomerOrderItem` from the Vendor Comparison report
(`vendor_comparison_service.py`, which deliberately never picks one itself).

A `VendorSelection` row is created or replaced per order line -- each
customer part has its own independent selection, keyed by
`customer_order_item_id` (unique), so selecting a vendor for one part never
touches another part's selection. Selections persist until the comparison
report is regenerated (i.e. a new customer order is uploaded); there is no
further lifecycle beyond that -- no Purchase Order concept, no locking.

Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import openpyxl
from openpyxl.styles import Font
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.ingestion.column_detector import decimal_to_string, normalise_part_number
from core.models import (
    CustomerOrderItem,
    InventoryImport,
    Vendor,
    VendorInventory,
    VendorSelection,
)


def _find_active_vendor_offer(
    vendor_id: int, normalized_part_number: str, session: Session
) -> VendorInventory | None:
    return session.execute(
        select(VendorInventory)
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .where(
            VendorInventory.vendor_id == vendor_id,
            VendorInventory.normalized_part_number == normalized_part_number,
            InventoryImport.is_active.is_(True),
        )
    ).scalar_one_or_none()


def upsert_selection(
    order_item_id: int, vendor_id: int, quantity_selected: Decimal, session: Session
) -> VendorSelection:
    """Create or replace the vendor selection for one order line. Always
    freely replaceable -- selecting a different vendor for this part simply
    overwrites the previous selection for that same part; it never affects
    any other part's selection.

    Raises `LookupError` if the order item or vendor doesn't exist, and
    `ValueError` if the vendor doesn't currently stock the part or
    `quantity_selected` exceeds what that vendor has available.
    """
    order_item = session.get(CustomerOrderItem, order_item_id)
    if order_item is None:
        raise LookupError(f"Customer order item {order_item_id} not found.")

    vendor = session.get(Vendor, vendor_id)
    if vendor is None:
        raise LookupError(f"Vendor {vendor_id} not found.")

    if quantity_selected <= 0:
        raise ValueError("quantity_selected must be greater than 0.")

    normalized = normalise_part_number(order_item.part_number_raw)
    offer = _find_active_vendor_offer(vendor_id, normalized, session)
    if offer is None:
        raise ValueError(
            f"Vendor '{vendor.name}' has no active inventory for part "
            f"{order_item.part_number_raw!r}."
        )

    if quantity_selected > offer.quantity_available:
        raise ValueError(
            f"Vendor '{vendor.name}' only has {offer.quantity_available} of "
            f"{order_item.part_number_raw!r} available (requested "
            f"{quantity_selected})."
        )

    if offer.part_id is None:
        raise ValueError(
            f"Vendor '{vendor.name}'s inventory row for "
            f"{order_item.part_number_raw!r} has no resolved Part -- re-import "
            "that vendor's inventory before selecting it."
        )

    selection = session.execute(
        select(VendorSelection).where(
            VendorSelection.customer_order_item_id == order_item_id
        )
    ).scalar_one_or_none()

    if selection is None:
        selection = VendorSelection(customer_order_item_id=order_item_id)
        session.add(selection)

    selection.vendor_id = vendor_id
    selection.part_id = offer.part_id
    selection.vendor_part_number = offer.vendor_part_number
    selection.quantity_selected = quantity_selected

    session.flush()
    return selection


def list_selections_for_order(order_id: int, session: Session) -> list[VendorSelection]:
    return list(
        session.execute(
            select(VendorSelection)
            .join(
                CustomerOrderItem,
                VendorSelection.customer_order_item_id == CustomerOrderItem.id,
            )
            .where(CustomerOrderItem.customer_order_id == order_id)
            .order_by(CustomerOrderItem.row_number)
        ).scalars()
    )


@dataclass
class SelectionExportRow:
    customer_part_number: str
    requested_quantity: Decimal
    vendor_name: str
    vendor_part_number: str
    available_quantity: Decimal | None


def list_selections_for_export(order_id: int, session: Session) -> list[SelectionExportRow]:
    """One row per part that currently has a vendor selected -- parts with
    no selection yet are simply omitted (only export what the user actually
    chose)."""
    selections = list_selections_for_order(order_id, session)

    rows: list[SelectionExportRow] = []
    for selection in selections:
        order_item = session.get(CustomerOrderItem, selection.customer_order_item_id)
        vendor = session.get(Vendor, selection.vendor_id)
        normalized = normalise_part_number(order_item.part_number_raw)
        offer = _find_active_vendor_offer(selection.vendor_id, normalized, session)

        rows.append(
            SelectionExportRow(
                customer_part_number=order_item.part_number_raw,
                requested_quantity=order_item.quantity_requested,
                vendor_name=vendor.name,
                vendor_part_number=selection.vendor_part_number,
                available_quantity=offer.quantity_available if offer is not None else None,
            )
        )

    return rows


EXPORT_HEADERS = [
    "Customer Part Number",
    "Requested Quantity",
    "Selected Vendor",
    "Vendor Part Number",
    "Available Quantity",
]


def _export_row_cells(row: SelectionExportRow) -> list[str]:
    return [
        row.customer_part_number,
        decimal_to_string(row.requested_quantity),
        row.vendor_name,
        row.vendor_part_number,
        decimal_to_string(row.available_quantity) if row.available_quantity is not None else "",
    ]


def to_export_workbook(rows: list[SelectionExportRow]) -> openpyxl.Workbook:
    """Vendor allocation sheet: "this customer part will be sourced from
    this vendor" -- no pricing, no vendor grouping, no PO numbering."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Selected Vendors"

    sheet.append(EXPORT_HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in rows:
        sheet.append(_export_row_cells(row))

    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value)) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = max_length + 2

    return workbook
