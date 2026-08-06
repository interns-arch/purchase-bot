"""Routes a `Classification` to the matching `core.services.*` import call.
Never duplicates that logic -- this is purely "which function do I call and
how do I normalize its result", per the requirement that the processing
engine must never reimplement business logic.

Duplicate-content detection is normalized into one shape for the caller
(`processor.py`), but the *mechanism* differs by document type and that
difference matters for correctness under `session.begin_nested()`:

- `run_customer_order_import` / `run_vendor_delivery_import` detect a
  duplicate and raise *before* writing anything -- safe to propagate as an
  exception from inside a nested transaction (there's nothing to roll back).
- `run_import` (inventory) is different: it always creates an
  `InventoryImport` row up front, and an unchanged re-upload is signaled by
  a normal return value (`status=AWAITING_CONFIRMATION`), which this module
  resolves by calling `cancel_import` -- a real state-changing write that
  must be allowed to commit. Raising an exception at that point would let
  the enclosing `session.begin_nested()` roll back the SAVEPOINT and undo
  that cancellation. So the inventory path reports its duplicate outcome
  via `DispatchResult.is_duplicate`, a normal return, not an exception.

Deliveries are matched by vendor + part directly, via
`vendor_delivery_service` -- this application has no Purchase Order
concept, so `core.services.delivery_import_service` (PO-based) is
deliberately NOT used here; it still serves the pre-existing CLI pipeline
unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.documents.models import IncomingDocumentType
from backend.app.integrations.google_sheets.sync_service import sync_vendor_inventory_to_sheet_safe
from backend.app.services.document_processor.detector import Classification
from core.logging_setup import get_logger
from core.models import ImportStatus
from core.services import customer_order_service as order_service
from core.services import inventory_import_service as import_service
from core.services import vendor_code_service
from core.services import vendor_delivery_service
from core.services import vendor_invoice_verification_service as invoice_service
from core.services import vendor_service

logger = get_logger(__name__)

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class DocumentAlreadyProcessedError(Exception):
    """Raised only for document types whose duplicate check happens before
    any writes (customer order, delivery) -- safe to raise from inside a
    nested transaction. See module docstring for why inventory is
    deliberately NOT one of these."""

    def __init__(self, existing_reference_id: int, message: str):
        self.existing_reference_id = existing_reference_id
        super().__init__(message)


class NotImplementedYetError(Exception):
    """Raised for document types with no import service yet (Vendor Invoice
    PDFs) -- the caller records this as UNSUPPORTED, never a crash."""


class UnknownVendorCodeError(Exception):
    """Raised when a filename carries a vendor-code-shaped prefix (e.g.
    "AR_CT") that doesn't match any registered vendor. Deliberately just a
    plain exception -- `processor.py`'s existing catch-all `except Exception`
    already marks the document FAILED with this message, which is exactly
    "reject with a meaningful error", so no special-case handling is needed
    upstream."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(
            f"Vendor code '{code}' is not registered. Ask your purchase team for your "
            "assigned code, or resend without a code prefix if this is your first upload."
        )


@dataclass
class DispatchResult:
    row_count: int = 0
    error_count: int = 0
    message: str | None = None
    vendor_id: int | None = None
    vendor_name: str | None = None
    inventory_import_id: int | None = None
    customer_order_id: int | None = None
    delivery_import_id: int | None = None
    invoice_verification_id: int | None = None
    is_duplicate: bool = False
    # The underlying `core.services.*` result's own status string (e.g.
    # "COMPLETED", "COMPLETED_WITH_ERRORS", or "FAILED" for zero-valid-rows)
    # -- callers that need the exact original status string (the manual
    # upload glue, to keep its response byte-identical to before this
    # refactor) read this instead of re-deriving it from row/error counts.
    core_status: str | None = None


def _vendor_name_from_filename(filename: str) -> str:
    return Path(filename).stem.strip()


def _onboard_new_vendor(name: str, session: Session) -> tuple["Vendor", str]:
    """First-time onboarding: a vendor's very first file may still be named
    with their real company name (no code prefix yet). Creates the vendor
    and auto-generates + permanently stores its Vendor Code, returned
    alongside the vendor so the caller can surface it (the team then hands
    that code to the vendor for every subsequent upload)."""
    vendor = vendor_service.create_vendor(name, session)
    code = vendor_code_service.generate_vendor_code(name, session)
    vendor.vendor_code = code
    session.flush()
    logger.info("New vendor onboarded: '%s' (id=%s, code=%s)", vendor.name, vendor.id, code)
    return vendor, code


def _dispatch_inventory(
    file_path: Path, classification: Classification, session: Session
) -> DispatchResult:
    onboarding_message: str | None = None

    if classification.vendor_id is not None:
        vendor = vendor_service.get_vendor(classification.vendor_id, session)
        if vendor is None:
            raise ValueError(f"Vendor {classification.vendor_id} does not exist.")
    elif classification.vendor_code is not None:
        # A code-shaped prefix was found in the filename but didn't match
        # any vendor -- reject rather than silently misrouting the file.
        raise UnknownVendorCodeError(classification.vendor_code)
    else:
        # No code-shaped prefix at all -- first-time onboarding by real name.
        name = _vendor_name_from_filename(file_path.name)
        existing = vendor_service.get_vendor_by_name(name, session)
        if existing is not None:
            vendor = existing
        else:
            vendor, code = _onboard_new_vendor(name, session)
            onboarding_message = f"New vendor '{vendor.name}' onboarded with code {code}."

    result = import_service.run_import(vendor.id, file_path, session)

    if result.status == ImportStatus.AWAITING_CONFIRMATION:
        # Same auto-skip behavior as the original manual-upload glue: an
        # unchanged re-upload is a no-op, not a pending confirmation. This
        # write must commit, so we return normally instead of raising --
        # see module docstring.
        import_service.cancel_import(result.import_id, session)
        return DispatchResult(
            vendor_id=vendor.id,
            vendor_name=vendor.name,
            inventory_import_id=result.import_id,
            is_duplicate=True,
            message=f"Unchanged since import #{result.duplicate_of_import_id}; skipped.",
        )

    if result.status != ImportStatus.FAILED:
        # Never allowed to fail this import -- see
        # `sync_vendor_inventory_to_sheet_safe`'s own docstring/try-except.
        # No-ops entirely when ENABLE_GOOGLE_SHEETS_SYNC is false. Covers
        # both manual upload and WhatsApp inventory import, since both
        # funnel through this one function.
        sync_vendor_inventory_to_sheet_safe(vendor.id, session)

    combined_message = (
        f"{onboarding_message} {result.message}" if onboarding_message and result.message else (
            onboarding_message or result.message
        )
    )

    return DispatchResult(
        row_count=result.row_count,
        error_count=result.error_count,
        message=combined_message,
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        inventory_import_id=result.import_id,
        core_status=result.status.value,
    )


def _dispatch_customer_order(file_path: Path, session: Session) -> DispatchResult:
    try:
        result = order_service.run_customer_order_import(file_path, session)
    except order_service.DuplicateCustomerOrderFileError as exc:
        raise DocumentAlreadyProcessedError(exc.existing_order_id, str(exc)) from exc

    return DispatchResult(
        row_count=result.row_count,
        error_count=result.error_count,
        customer_order_id=result.order_id,
        core_status=result.status.value,
    )


def _dispatch_delivery(file_path: Path, session: Session) -> DispatchResult:
    try:
        result = vendor_delivery_service.run_vendor_delivery_import(file_path, session)
    except vendor_delivery_service.DuplicateVendorDeliveryFileError as exc:
        raise DocumentAlreadyProcessedError(exc.existing_import_id, str(exc)) from exc

    return DispatchResult(
        row_count=result.row_count,
        error_count=result.error_count,
        delivery_import_id=result.import_id,
        core_status=result.status.value,
    )


def _dispatch_vendor_invoice(file_path: Path, session: Session) -> DispatchResult:
    try:
        result = invoice_service.run_invoice_verification(file_path, session)
    except invoice_service.DuplicateVendorInvoiceFileError as exc:
        raise DocumentAlreadyProcessedError(exc.existing_import_id, str(exc)) from exc

    return DispatchResult(
        row_count=result.row_count,
        error_count=result.error_count,
        message=result.message,
        vendor_id=result.vendor_id,
        vendor_name=result.vendor_name,
        invoice_verification_id=result.invoice_import_id,
        core_status=result.status.value,
    )


def dispatch(file_path: Path, classification: Classification, session: Session) -> DispatchResult:
    if classification.document_type == IncomingDocumentType.VENDOR_INVENTORY:
        return _dispatch_inventory(file_path, classification, session)
    if classification.document_type == IncomingDocumentType.CUSTOMER_ORDER:
        return _dispatch_customer_order(file_path, session)
    if classification.document_type == IncomingDocumentType.DELIVERY:
        return _dispatch_delivery(file_path, session)
    if classification.document_type == IncomingDocumentType.VENDOR_INVOICE:
        return _dispatch_vendor_invoice(file_path, session)
    raise NotImplementedYetError(f"No dispatcher for document type {classification.document_type}.")
