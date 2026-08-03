"""Orchestrates the delivery-import workflow: validate, resolve against the
Purchase Order they claim to fulfil, and store `DeliveryItem` rows.

Pure business logic -- no `print()`/`input()` here. The CLI layer
(`delivery_import.py`) is the only place that talks to a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.hashing import sha256_of_file
from core.ingestion.column_detector import (
    find_delivery_columns,
    is_parseable_quantity,
    normalise_part_number,
    parse_quantity,
)
from core.ingestion.csv_reader import read_csv_rows
from core.ingestion.excel_reader import read_excel_rows
from core.ingestion.types import ParsedFile
from core.models import (
    DeliveryImport,
    DeliveryImportError,
    DeliveryImportStatus,
    DeliveryItem,
    Part,
    PartAlias,
    PurchaseOrder,
    PurchaseOrderItem,
    Vendor,
)

SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls"}


class DuplicateDeliveryFileError(Exception):
    """Raised when this exact file (name + content) was already imported."""

    def __init__(self, existing_import_id: int):
        self.existing_import_id = existing_import_id
        super().__init__(
            f"This file's content was already imported as delivery import #{existing_import_id}."
        )


@dataclass
class DeliveryImportResult:
    import_id: int
    file_name: str
    status: DeliveryImportStatus
    row_count: int
    error_count: int
    errors: list[str] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _read_file(file_path: Path) -> ParsedFile:
    if file_path.suffix.lower() == ".csv":
        return read_csv_rows(file_path)
    return read_excel_rows(file_path)


def _resolve_vendor(raw_name: str, session: Session) -> Vendor | None:
    raw_name = raw_name.strip()
    if not raw_name:
        return None
    return session.execute(
        select(Vendor).where(func.lower(Vendor.name) == raw_name.lower())
    ).scalar_one_or_none()


def _resolve_purchase_order(raw_po_number: str, session: Session) -> PurchaseOrder | None:
    raw_po_number = raw_po_number.strip()
    if not raw_po_number:
        return None
    return session.execute(
        select(PurchaseOrder).where(func.lower(PurchaseOrder.po_number) == raw_po_number.lower())
    ).scalar_one_or_none()


def _resolve_part(vendor_id: int, raw_part_number: str, session: Session) -> Part | None:
    normalized = normalise_part_number(raw_part_number)
    if not normalized:
        return None

    alias = session.execute(
        select(PartAlias).where(
            PartAlias.vendor_id == vendor_id,
            PartAlias.normalized_part_number == normalized,
        )
    ).scalar_one_or_none()
    if alias is not None:
        return alias.part

    return session.execute(
        select(Part).where(Part.canonical_part_number == normalized)
    ).scalar_one_or_none()


def _find_po_item(
    po_id: int, part_id: int, session: Session
) -> PurchaseOrderItem | None:
    return session.execute(
        select(PurchaseOrderItem).where(
            PurchaseOrderItem.po_id == po_id, PurchaseOrderItem.part_id == part_id
        )
    ).scalar_one_or_none()


def run_delivery_import(file_path: Path, session: Session) -> DeliveryImportResult:
    """Import one delivery file. Raises `DuplicateDeliveryFileError` if this
    exact file content was already imported successfully."""
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))

    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file extension '{file_path.suffix}'. "
            f"Supported: {sorted(SUPPORTED_EXTENSIONS)}."
        )

    file_size = file_path.stat().st_size
    if file_size == 0:
        raise ValueError(f"'{file_path.name}' is empty.")

    content_hash = sha256_of_file(file_path)

    existing = session.execute(
        select(DeliveryImport).where(
            DeliveryImport.file_name == file_path.name,
            DeliveryImport.content_hash == content_hash,
            DeliveryImport.status.in_(
                (DeliveryImportStatus.COMPLETED, DeliveryImportStatus.COMPLETED_WITH_ERRORS)
            ),
        )
    ).scalar_one_or_none()

    if existing is not None:
        raise DuplicateDeliveryFileError(existing.id)

    parsed_file = _read_file(file_path)
    vendor_column, po_column, part_column, quantity_column = find_delivery_columns(
        parsed_file.headers, file_path.name
    )

    delivery_import = DeliveryImport(
        file_name=file_path.name,
        file_size_bytes=file_size,
        content_hash=content_hash,
        status=DeliveryImportStatus.COMPLETED,
        row_count=0,
        error_count=0,
    )
    session.add(delivery_import)
    session.flush()  # assign delivery_import.id

    row_count = 0
    error_count = 0
    error_messages: list[str] = []

    def _log_error(row_number: int, row: dict, reason: str, detail: str) -> None:
        nonlocal error_count
        session.add(
            DeliveryImportError(
                delivery_import_id=delivery_import.id,
                row_number=row_number,
                raw_row=row,
                error_reason=reason,
                error_detail=detail,
            )
        )
        error_count += 1
        error_messages.append(f"Row {row_number}: {reason} -- {detail}")

    for row_number, row in enumerate(parsed_file.rows, start=2):
        raw_vendor = row.get(vendor_column, "")
        raw_po_number = row.get(po_column, "")
        raw_part_number = row.get(part_column, "")
        raw_quantity = row.get(quantity_column, "")

        if not raw_vendor.strip():
            _log_error(row_number, row, "INVALID_VENDOR", "Vendor is blank.")
            continue

        if not raw_po_number.strip():
            _log_error(row_number, row, "INVALID_PO_NUMBER", "PO Number is blank.")
            continue

        if not raw_part_number.strip():
            _log_error(row_number, row, "INVALID_PART_NUMBER", "Part Number is blank.")
            continue

        if not is_parseable_quantity(raw_quantity):
            _log_error(
                row_number,
                row,
                "INVALID_QUANTITY",
                f"Could not parse delivered quantity {raw_quantity!r}.",
            )
            continue

        quantity_delivered = parse_quantity(raw_quantity)
        if quantity_delivered < 0:
            _log_error(
                row_number, row, "INVALID_QUANTITY", "Delivered quantity cannot be negative."
            )
            continue

        vendor = _resolve_vendor(raw_vendor, session)
        if vendor is None:
            _log_error(
                row_number, row, "VENDOR_NOT_FOUND", f"No vendor named {raw_vendor!r}."
            )
            continue

        purchase_order = _resolve_purchase_order(raw_po_number, session)
        if purchase_order is None:
            _log_error(
                row_number,
                row,
                "PO_NOT_FOUND",
                f"No purchase order {raw_po_number!r}.",
            )
            continue

        if purchase_order.vendor_id != vendor.id:
            _log_error(
                row_number,
                row,
                "PO_VENDOR_MISMATCH",
                f"PO {raw_po_number!r} belongs to vendor "
                f"'{purchase_order.vendor.name}', not {raw_vendor!r}.",
            )
            continue

        part = _resolve_part(vendor.id, raw_part_number, session)
        if part is None:
            _log_error(
                row_number,
                row,
                "PART_NOT_FOUND",
                f"Part {raw_part_number!r} is not a known part for vendor {raw_vendor!r}.",
            )
            continue

        po_item = _find_po_item(purchase_order.id, part.id, session)
        if po_item is None:
            _log_error(
                row_number,
                row,
                "PART_NOT_IN_PO",
                f"Part {raw_part_number!r} was not ordered on PO {raw_po_number!r}.",
            )
            continue

        session.add(
            DeliveryItem(
                delivery_import_id=delivery_import.id,
                po_id=purchase_order.id,
                po_item_id=po_item.id,
                vendor_id=vendor.id,
                part_id=part.id,
                row_number=row_number,
                po_number_raw=raw_po_number.strip(),
                vendor_part_number=raw_part_number.strip(),
                quantity_delivered=quantity_delivered,
                raw_data=row,
            )
        )
        row_count += 1

    delivery_import.row_count = row_count
    delivery_import.error_count = error_count
    delivery_import.status = (
        DeliveryImportStatus.FAILED
        if row_count == 0 and error_count > 0
        else DeliveryImportStatus.COMPLETED_WITH_ERRORS
        if error_count > 0
        else DeliveryImportStatus.COMPLETED
    )
    delivery_import.completed_at = _utcnow()
    session.flush()

    return DeliveryImportResult(
        import_id=delivery_import.id,
        file_name=file_path.name,
        status=delivery_import.status,
        row_count=row_count,
        error_count=error_count,
        errors=error_messages,
    )


def list_delivery_import_history(session: Session) -> list[DeliveryImport]:
    return list(
        session.execute(
            select(DeliveryImport).order_by(DeliveryImport.created_at.desc())
        ).scalars()
    )


def list_delivery_import_errors(
    delivery_import_id: int, session: Session
) -> list[DeliveryImportError]:
    return list(
        session.execute(
            select(DeliveryImportError)
            .where(DeliveryImportError.delivery_import_id == delivery_import_id)
            .order_by(DeliveryImportError.id)
        ).scalars()
    )
