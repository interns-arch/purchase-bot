"""For every Purchase Order line with a pending quantity, search the
currently active inventory of every OTHER vendor for the same part.

Reuses `gap_analysis_service.compute_gap_analysis` (so this script is
independently runnable -- it doesn't depend on `gap_analysis.py` having
been run first) and `inventory_import_service`'s active-import concept.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.ingestion.column_detector import normalise_header
from core.models import InventoryImport, VendorInventory
from core.services.gap_analysis_service import GapRow, compute_gap_analysis

_PRICE_LIKE_HEADERS = {"price", "unitprice", "rate", "mrp", "sellingprice"}


@dataclass
class AlternativeVendorRow:
    part_number: str
    pending_qty: Decimal
    alternative_vendor: str
    available_quantity: Decimal
    price: Decimal | None
    inventory_file: str


def _extract_price(inventory_row: VendorInventory) -> Decimal | None:
    if inventory_row.price is not None:
        return inventory_row.price
    if inventory_row.mrp is not None:
        return inventory_row.mrp

    for key, value in (inventory_row.raw_data or {}).items():
        if normalise_header(key) in _PRICE_LIKE_HEADERS:
            try:
                return Decimal(str(value).strip().replace(",", ""))
            except InvalidOperation:
                continue

    return None


def find_alternatives_for_gap_row(gap_row: GapRow, session: Session) -> list[AlternativeVendorRow]:
    """Other vendors' active inventory for `gap_row.part_id`, excluding the
    vendor already on the hook for this PO line, sorted by highest available
    quantity first."""
    candidates = session.execute(
        select(VendorInventory, InventoryImport)
        .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
        .where(
            VendorInventory.part_id == gap_row.part_id,
            VendorInventory.vendor_id != gap_row.vendor_id,
            InventoryImport.is_active.is_(True),
            VendorInventory.quantity_available > 0,
        )
    ).all()

    results = [
        AlternativeVendorRow(
            part_number=gap_row.part_number,
            pending_qty=gap_row.pending_qty,
            alternative_vendor=inventory_row.vendor.name,
            available_quantity=inventory_row.quantity_available,
            price=_extract_price(inventory_row),
            inventory_file=inventory_import.file_name,
        )
        for inventory_row, inventory_import in candidates
    ]

    results.sort(key=lambda row: row.available_quantity, reverse=True)
    return results


def find_all_alternatives(session: Session) -> list[AlternativeVendorRow]:
    gap_rows = compute_gap_analysis(session)
    pending_rows = [row for row in gap_rows if row.pending_qty > 0]

    alternatives: list[AlternativeVendorRow] = []
    for gap_row in pending_rows:
        alternatives.extend(find_alternatives_for_gap_row(gap_row, session))

    return alternatives
