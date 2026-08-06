"""Vendor Selection: lets the user (or the automatic rule engine, see
`core.services.rules.engine`) pick one or more vendors + quantities per
`CustomerOrderItem` from the Vendor Comparison report
(`vendor_comparison_service.py`, which deliberately never picks one itself).

A `VendorSelection` row is created or replaced per (order line, vendor)
pair -- each customer part can be split across several vendors when no
single vendor can cover the full requested quantity, but selecting/removing
one vendor's allocation never touches another vendor's allocation for the
same line, or another line's selections. Selections persist until the
comparison report is regenerated (i.e. a new customer order is uploaded);
there is no further lifecycle beyond that -- no Purchase Order concept, no
locking.

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


def _selections_for_item(
    order_item_id: int, session: Session, *, exclude_vendor_id: int | None = None
) -> list[VendorSelection]:
    statement = select(VendorSelection).where(
        VendorSelection.customer_order_item_id == order_item_id
    )
    if exclude_vendor_id is not None:
        statement = statement.where(VendorSelection.vendor_id != exclude_vendor_id)
    return list(session.execute(statement).scalars())


def list_selections_for_item(order_item_id: int, session: Session) -> list[VendorSelection]:
    """All of one order line's current vendor allocations (there can be
    several when a line is split across vendors)."""
    return _selections_for_item(order_item_id, session)


def upsert_selection(
    order_item_id: int, vendor_id: int, quantity_selected: Decimal, session: Session
) -> VendorSelection:
    """Create or replace this vendor's allocation for one order line. An
    order line may have allocations from several vendors at once (e.g. when
    no single vendor can cover the full requested quantity) -- re-selecting
    the SAME vendor for the SAME line overwrites just that vendor's own
    allocation; it never touches any other vendor's allocation for the same
    line, or any other line's selections.

    Raises `LookupError` if the order item or vendor doesn't exist, and
    `ValueError` if the vendor doesn't currently stock the part,
    `quantity_selected` exceeds what that vendor has available, or the sum
    of all vendors' allocations for this line would exceed the requested
    quantity.
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

    other_selections = _selections_for_item(
        order_item_id, session, exclude_vendor_id=vendor_id
    )
    already_allocated = sum(
        (other.quantity_selected for other in other_selections), Decimal(0)
    )
    if already_allocated + quantity_selected > order_item.quantity_requested:
        raise ValueError(
            f"Allocating {quantity_selected} to '{vendor.name}' would bring the total "
            f"selected for {order_item.part_number_raw!r} to "
            f"{already_allocated + quantity_selected}, exceeding the requested quantity "
            f"of {order_item.quantity_requested}."
        )

    selection = session.execute(
        select(VendorSelection).where(
            VendorSelection.customer_order_item_id == order_item_id,
            VendorSelection.vendor_id == vendor_id,
        )
    ).scalar_one_or_none()

    if selection is None:
        selection = VendorSelection(
            customer_order_item_id=order_item_id, vendor_id=vendor_id
        )
        session.add(selection)

    selection.part_id = offer.part_id
    selection.vendor_part_number = offer.vendor_part_number
    selection.quantity_selected = quantity_selected

    session.flush()
    return selection


def clear_selections_for_item(order_item_id: int, session: Session) -> None:
    """Remove every vendor's allocation for one order line. Used by the
    automatic rule engine before applying a fresh set of allocations, so
    re-running (or switching strategies) replaces the previous automatic
    picks instead of leaving stale vendors from an earlier run alongside the
    new ones."""
    for selection in _selections_for_item(order_item_id, session):
        session.delete(selection)
    session.flush()


def remove_selection(order_item_id: int, vendor_id: int, session: Session) -> None:
    """Remove one vendor's allocation from an order line, leaving any other
    vendors' allocations for that same line untouched. A no-op if that
    vendor had no allocation for this line."""
    selection = session.execute(
        select(VendorSelection).where(
            VendorSelection.customer_order_item_id == order_item_id,
            VendorSelection.vendor_id == vendor_id,
        )
    ).scalar_one_or_none()
    if selection is None:
        return
    session.delete(selection)
    session.flush()


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
