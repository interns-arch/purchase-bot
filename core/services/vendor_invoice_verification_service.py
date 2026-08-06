"""Vendor Invoice Verification: extracts vendor name + line items from a
Vendor Invoice PDF (`invoice_extraction_service`), resolves each line
against the vendor's current `VendorSelection` allocations, classifies the
discrepancy (matched / short / extra / missing / unexpected supply), and
persists both the verification result itself AND matching
`VendorDeliveryItem` rows -- so Delivery Tracking
(`delivery_tracking_service.py`) and Vendor Performance
(`vendor_performance_tracking_service.py`) pick up invoice-verified
deliveries automatically, with ZERO changes to either of those services
(both are pure aggregations over `VendorDeliveryItem` / `VendorSelection`).

Mirrors `vendor_delivery_service.py`'s shape (content-hash dedupe, header +
line-item tables) for consistency. Pure business logic -- no
FastAPI/print()/input() here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.hashing import sha256_of_file
from core.ingestion.column_detector import normalise_part_number
from core.models import (
    DeliveryImportStatus,
    InvoiceLineDiscrepancyType,
    InvoiceVerificationStatus,
    Part,
    PartAlias,
    Vendor,
    VendorDeliveryImport,
    VendorDeliveryImportSource,
    VendorDeliveryItem,
    VendorInvoiceImport,
    VendorInvoiceLineResult,
    VendorSelection,
)
from core.services.invoice_extraction_service import InvoiceParsingError, extract_invoice_data

SUPPORTED_EXTENSIONS = {".pdf"}


class DuplicateVendorInvoiceFileError(Exception):
    """Raised when this exact file (name + content) was already verified."""

    def __init__(self, existing_import_id: int):
        self.existing_import_id = existing_import_id
        super().__init__(
            f"This file's content was already imported as invoice import #{existing_import_id}."
        )


@dataclass
class VendorInvoiceVerificationResult:
    invoice_import_id: int
    file_name: str
    status: InvoiceVerificationStatus
    vendor_id: int | None
    vendor_name: str | None
    row_count: int
    error_count: int
    message: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _vendor_name_from_filename(filename: str) -> str:
    return Path(filename).stem.strip()


def _resolve_vendor(extracted_name: str | None, filename: str, session: Session) -> Vendor | None:
    for candidate in (extracted_name, _vendor_name_from_filename(filename)):
        if not candidate:
            continue
        vendor = session.execute(
            select(Vendor).where(func.lower(Vendor.name) == candidate.strip().lower())
        ).scalar_one_or_none()
        if vendor is not None:
            return vendor
    return None


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


def _expected_quantities_by_part(vendor_id: int, session: Session) -> dict[int, Decimal]:
    """Sums this vendor's current `VendorSelection` allocations by part --
    what the purchase team (manual or the rule engine) currently expects
    this vendor to deliver, across every customer order line they were
    selected for."""
    rows = session.execute(
        select(VendorSelection.part_id, VendorSelection.quantity_selected).where(
            VendorSelection.vendor_id == vendor_id
        )
    ).all()
    totals: dict[int, Decimal] = {}
    for part_id, quantity in rows:
        totals[part_id] = totals.get(part_id, Decimal(0)) + quantity
    return totals


def _early_result(
    invoice_import: VendorInvoiceImport, *, vendor_name: str | None
) -> VendorInvoiceVerificationResult:
    return VendorInvoiceVerificationResult(
        invoice_import_id=invoice_import.id,
        file_name=invoice_import.file_name,
        status=invoice_import.status,
        vendor_id=invoice_import.vendor_id,
        vendor_name=vendor_name,
        row_count=0,
        error_count=0,
        message=invoice_import.error_message,
    )


def run_invoice_verification(file_path: Path, session: Session) -> VendorInvoiceVerificationResult:
    """Raises `DuplicateVendorInvoiceFileError` if this exact file content
    was already verified successfully -- checked BEFORE any row is
    written, so it's safe to raise from inside a nested transaction. Never
    raises for a PDF that fails to parse or resolve to a known vendor --
    those are recorded as a NEEDS_REVIEW result instead, so one bad invoice
    never blocks the rest of a batch."""
    if not file_path.exists():
        raise FileNotFoundError(str(file_path))
    if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file extension '{file_path.suffix}'. Supported: {sorted(SUPPORTED_EXTENSIONS)}."
        )

    file_size = file_path.stat().st_size
    if file_size == 0:
        raise ValueError(f"'{file_path.name}' is empty.")

    content_hash = sha256_of_file(file_path)

    existing = session.execute(
        select(VendorInvoiceImport).where(
            VendorInvoiceImport.file_name == file_path.name,
            VendorInvoiceImport.content_hash == content_hash,
            VendorInvoiceImport.status.in_(
                (InvoiceVerificationStatus.COMPLETED, InvoiceVerificationStatus.COMPLETED_WITH_ERRORS)
            ),
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise DuplicateVendorInvoiceFileError(existing.id)

    invoice_import = VendorInvoiceImport(
        file_name=file_path.name,
        file_size_bytes=file_size,
        content_hash=content_hash,
        status=InvoiceVerificationStatus.NEEDS_REVIEW,
    )
    session.add(invoice_import)
    session.flush()  # assign invoice_import.id

    try:
        extracted = extract_invoice_data(file_path)
    except InvoiceParsingError as exc:
        invoice_import.error_message = str(exc)
        invoice_import.completed_at = _utcnow()
        session.flush()
        return _early_result(invoice_import, vendor_name=None)

    invoice_import.vendor_name_extracted = extracted.vendor_name
    vendor = _resolve_vendor(extracted.vendor_name, file_path.name, session)
    if vendor is None:
        invoice_import.error_message = (
            f"Could not resolve a known vendor from this invoice "
            f"(extracted name: {extracted.vendor_name!r})."
        )
        invoice_import.completed_at = _utcnow()
        session.flush()
        return _early_result(invoice_import, vendor_name=extracted.vendor_name)

    invoice_import.vendor_id = vendor.id

    expected_by_part = _expected_quantities_by_part(vendor.id, session)
    matched_part_ids: set[int] = set()
    delivery_import: VendorDeliveryImport | None = None

    def _get_delivery_import() -> VendorDeliveryImport:
        nonlocal delivery_import
        if delivery_import is None:
            delivery_import = VendorDeliveryImport(
                file_name=file_path.name,
                file_size_bytes=file_size,
                content_hash=content_hash,
                status=DeliveryImportStatus.COMPLETED,
                source=VendorDeliveryImportSource.VENDOR_INVOICE_PDF,
                row_count=0,
                error_count=0,
            )
            session.add(delivery_import)
            session.flush()  # assign delivery_import.id
        return delivery_import

    row_count = 0
    error_count = 0

    for row_number, line in enumerate(extracted.lines, start=1):
        part = _resolve_part(vendor.id, line.part_number, session)

        if part is None:
            session.add(
                VendorInvoiceLineResult(
                    invoice_import_id=invoice_import.id,
                    part_number_raw=line.part_number,
                    quantity_invoiced=line.quantity,
                    expected_quantity=None,
                    discrepancy_type=InvoiceLineDiscrepancyType.UNEXPECTED_PART,
                    raw_text=line.raw_text,
                )
            )
            error_count += 1
            continue

        expected_quantity = expected_by_part.get(part.id)
        matched_part_ids.add(part.id)

        if expected_quantity is None:
            discrepancy = InvoiceLineDiscrepancyType.UNEXPECTED_PART
        elif line.quantity < expected_quantity:
            discrepancy = InvoiceLineDiscrepancyType.SHORT_SUPPLY
        elif line.quantity > expected_quantity:
            discrepancy = InvoiceLineDiscrepancyType.EXTRA_SUPPLY
        else:
            discrepancy = InvoiceLineDiscrepancyType.MATCHED

        session.add(
            VendorInvoiceLineResult(
                invoice_import_id=invoice_import.id,
                part_number_raw=line.part_number,
                part_id=part.id,
                quantity_invoiced=line.quantity,
                expected_quantity=expected_quantity,
                discrepancy_type=discrepancy,
                raw_text=line.raw_text,
            )
        )
        row_count += 1
        if discrepancy != InvoiceLineDiscrepancyType.MATCHED:
            error_count += 1

        # Mirror into VendorDeliveryItem so Delivery Tracking / Vendor
        # Performance reconcile this invoice's delivered quantity exactly
        # like any manually uploaded delivery file -- no changes needed to
        # either of those services.
        delivery = _get_delivery_import()
        session.add(
            VendorDeliveryItem(
                vendor_delivery_import_id=delivery.id,
                vendor_id=vendor.id,
                part_id=part.id,
                vendor_part_number=line.part_number,
                quantity_delivered=line.quantity,
                delivery_date=None,
                row_number=row_number,
                raw_data={"raw_text": line.raw_text, "source": "vendor_invoice_pdf"},
            )
        )

    # Parts the purchase team expected from this vendor but that never
    # showed up on the invoice at all -- MISSING_PART. These don't produce
    # a VendorDeliveryItem (nothing was delivered); Delivery Tracking
    # already reports them as under-delivered via the existing
    # ordered-vs-delivered gap -- this is purely for the invoice's own
    # discrepancy report.
    for part_id, expected_quantity in expected_by_part.items():
        if part_id in matched_part_ids:
            continue
        part = session.get(Part, part_id)
        session.add(
            VendorInvoiceLineResult(
                invoice_import_id=invoice_import.id,
                part_number_raw=part.canonical_part_number if part else str(part_id),
                part_id=part_id,
                quantity_invoiced=Decimal(0),
                expected_quantity=expected_quantity,
                discrepancy_type=InvoiceLineDiscrepancyType.MISSING_PART,
                raw_text=None,
            )
        )
        error_count += 1

    if delivery_import is not None:
        delivery_import.row_count = row_count
        delivery_import.completed_at = _utcnow()
        invoice_import.vendor_delivery_import_id = delivery_import.id

    invoice_import.row_count = row_count
    invoice_import.error_count = error_count
    invoice_import.status = (
        InvoiceVerificationStatus.COMPLETED_WITH_ERRORS
        if error_count > 0
        else InvoiceVerificationStatus.COMPLETED
    )
    invoice_import.completed_at = _utcnow()
    session.flush()

    return VendorInvoiceVerificationResult(
        invoice_import_id=invoice_import.id,
        file_name=file_path.name,
        status=invoice_import.status,
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        row_count=row_count,
        error_count=error_count,
    )


def list_invoice_imports(session: Session) -> list[VendorInvoiceImport]:
    return list(
        session.execute(
            select(VendorInvoiceImport).order_by(VendorInvoiceImport.created_at.desc())
        ).scalars()
    )


def get_invoice_import(invoice_import_id: int, session: Session) -> VendorInvoiceImport | None:
    return session.get(VendorInvoiceImport, invoice_import_id)


def list_invoice_line_results(
    invoice_import_id: int, session: Session
) -> list[VendorInvoiceLineResult]:
    return list(
        session.execute(
            select(VendorInvoiceLineResult)
            .where(VendorInvoiceLineResult.invoice_import_id == invoice_import_id)
            .order_by(VendorInvoiceLineResult.id)
        ).scalars()
    )
