"""Generates vendor-specific Purchase Orders from a completed order-matching
run (`output/matching_output.csv`).

This is a bridging step: `order_matching.py` (already completed) only
produces a flat CSV of matched/partial/unfulfilled order lines -- it does
not persist Purchase Orders anywhere. Delivery tracking, gap analysis and
vendor performance all need real `PurchaseOrder` / `PurchaseOrderItem`
rows to compare deliveries against, so this service turns each vendor's
MATCHED/PARTIAL lines from that CSV into one `PurchaseOrder` per vendor.

Pure business logic -- no `print()`/`input()` here; see `po_generator.py`
for the CLI layer. The web application no longer has a Purchase Order
concept (see `vendor_selection_service.py`'s "Export Selected Vendors"
instead) -- this module is kept only for the pre-existing CLI pipeline.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.hashing import sha256_of_file
from core.ingestion.column_detector import normalise_part_number
from core.models import Part, PurchaseOrder, PurchaseOrderItem

FULFILLABLE_STATUSES = {"MATCHED", "PARTIAL"}
_PO_NUMBER_PATTERN = re.compile(r"^PO(\d+)$")


class MatchingOutputNotFoundError(Exception):
    """Raised when the matching_output.csv file doesn't exist yet."""


@dataclass
class POGenerationResult:
    already_generated: bool
    purchase_orders_created: list[str] = field(default_factory=list)
    items_created: int = 0
    skipped_rows: int = 0
    skip_reasons: list[str] = field(default_factory=list)


def _next_po_number(session: Session) -> str:
    existing_numbers = session.execute(select(PurchaseOrder.po_number)).scalars().all()

    highest = 0
    for po_number in existing_numbers:
        match = _PO_NUMBER_PATTERN.match(po_number)
        if match:
            highest = max(highest, int(match.group(1)))

    return f"PO{highest + 1:03d}"


def _read_matching_rows(matching_csv_path: Path) -> list[dict[str, str]]:
    with matching_csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def _existing_vendor_part_pairs(session: Session) -> set[tuple[int, int]]:
    """(vendor_id, part_id) pairs that already have a `PurchaseOrderItem`,
    across every previously generated PO -- regardless of which
    matching_output.csv content produced them."""
    return set(
        session.execute(
            select(PurchaseOrder.vendor_id, PurchaseOrderItem.part_id).join(
                PurchaseOrderItem, PurchaseOrderItem.po_id == PurchaseOrder.id
            )
        ).all()
    )


def generate_purchase_orders(
    matching_csv_path: Path, session: Session
) -> POGenerationResult:
    """Group MATCHED/PARTIAL rows from `matching_csv_path` by vendor and
    create one new `PurchaseOrder` per vendor holding only the
    `PurchaseOrderItem`s for (vendor, part) combinations that don't already
    have one from a prior run.

    Idempotent at two levels: an exact-content re-run is a fast no-op
    (content hash match), and a changed matching_output.csv (e.g. a new
    customer order line) still won't duplicate PO items for vendor+part
    combinations that were already turned into a PO by an earlier run.
    """
    if not matching_csv_path.exists():
        raise MatchingOutputNotFoundError(str(matching_csv_path))

    content_hash = sha256_of_file(matching_csv_path)

    already_generated = session.execute(
        select(PurchaseOrder.id).where(
            PurchaseOrder.source_content_hash == content_hash
        )
    ).first()

    if already_generated is not None:
        return POGenerationResult(already_generated=True)

    existing_vendor_part_pairs = _existing_vendor_part_pairs(session)
    rows = _read_matching_rows(matching_csv_path)

    rows_by_vendor: dict[int, list[dict[str, str]]] = {}
    skipped_rows = 0
    skip_reasons: list[str] = []

    for row in rows:
        status = (row.get("match_status") or "").strip().upper()
        if status not in FULFILLABLE_STATUSES:
            continue

        vendor_id_raw = (row.get("vendor_id") or "").strip()
        try:
            fulfilled_qty = Decimal((row.get("quantity_fulfilled") or "0").strip())
        except InvalidOperation:
            fulfilled_qty = Decimal("0")

        if not vendor_id_raw or fulfilled_qty <= 0:
            skipped_rows += 1
            skip_reasons.append(
                f"part={row.get('part_number')!r}: missing vendor or non-positive "
                f"fulfilled quantity ({row.get('quantity_fulfilled')!r})"
            )
            continue

        rows_by_vendor.setdefault(int(vendor_id_raw), []).append(row)

    result = POGenerationResult(
        already_generated=False, skipped_rows=skipped_rows, skip_reasons=skip_reasons
    )

    for vendor_id in sorted(rows_by_vendor):
        vendor_rows = rows_by_vendor[vendor_id]

        new_items: list[tuple[Part, dict[str, str]]] = []
        for row in vendor_rows:
            normalized = normalise_part_number(row.get("part_number"))
            part = session.execute(
                select(Part).where(Part.canonical_part_number == normalized)
            ).scalar_one_or_none()

            if part is None:
                result.skipped_rows += 1
                result.skip_reasons.append(
                    f"part={row.get('part_number')!r}: no canonical Part found "
                    f"(was it really matched by order_matching.py?)"
                )
                continue

            if (vendor_id, part.id) in existing_vendor_part_pairs:
                result.skipped_rows += 1
                result.skip_reasons.append(
                    f"vendor_id={vendor_id}, part={row.get('part_number')!r}: "
                    f"already has a purchase order item from a prior run, skipped."
                )
                continue

            new_items.append((part, row))
            existing_vendor_part_pairs.add((vendor_id, part.id))

        if not new_items:
            continue

        po_number = _next_po_number(session)
        purchase_order = PurchaseOrder(
            po_number=po_number,
            vendor_id=vendor_id,
            source_file=matching_csv_path.name,
            source_content_hash=content_hash,
        )
        session.add(purchase_order)
        session.flush()  # assign purchase_order.id

        for part, row in new_items:
            session.add(
                PurchaseOrderItem(
                    po_id=purchase_order.id,
                    part_id=part.id,
                    vendor_part_number=(row.get("vendor_part_number") or "").strip()
                    or row.get("part_number", ""),
                    quantity_ordered=Decimal(row["quantity_fulfilled"]),
                )
            )
            result.items_created += 1

        result.purchase_orders_created.append(po_number)

    session.flush()
    return result


def list_purchase_orders(session: Session) -> list[PurchaseOrder]:
    return list(
        session.execute(select(PurchaseOrder).order_by(PurchaseOrder.po_number)).scalars()
    )
