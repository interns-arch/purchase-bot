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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.app.documents.models import IncomingDocumentType
from backend.app.integrations.google_sheets.sync_service import sync_vendor_inventory_to_sheet_safe
from backend.app.services.document_processor.detector import Classification
from core.logging_setup import get_logger
from core.models import ImportStatus
from core.services import customer_code_service
from core.services import customer_order_service as order_service
from core.services import customer_service
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


class UnknownCustomerCodeError(Exception):
    """Raised when a filename carries a customer-code-shaped prefix (e.g.
    "AB_CO") that doesn't match any registered customer. Deliberately just a
    plain exception -- mirrors `UnknownVendorCodeError` exactly, so
    `processor.py`'s existing catch-all `except Exception` already marks the
    document FAILED with this message instead of silently assigning the
    order to the wrong customer."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(
            f"Customer code '{code}' is not registered. Ask your team for the correct "
            "code, or resend without a code prefix if this is a new customer's first order."
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
    customer_id: int | None = None
    customer_name: str | None = None
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


def _resolve_or_onboard_vendor(name: str, session: Session) -> tuple["Vendor", str | None]:
    """Reuse the existing vendor for this company NAME (the stable identity --
    never the generated code), or onboard a brand-new one. Race-safe:

    - Reuse is decided first by an exact (case-insensitive) name lookup. An
      existing vendor is returned unchanged -- same vendor_id, same
      vendor_code, no new code generated. Returns onboarding_message=None.
    - Only a genuinely new name is onboarded (create vendor + generate ONE
      code). If two files for the same new vendor are processed almost
      simultaneously, the unique(lower(name)) constraint makes the second
      INSERT fail; we recover inside a SAVEPOINT and reuse the vendor the
      winning transaction created, so only ONE vendor / ONE code ever exists
      and both imports share it -- never a spurious _2/_3 code.
    """
    existing = vendor_service.get_vendor_by_name(name, session)
    if existing is not None:
        return existing, None  # reuse existing vendor + code, unchanged

    try:
        with session.begin_nested():  # SAVEPOINT: undoable if the race is lost
            vendor = vendor_service.create_vendor(name, session)
            code = vendor_code_service.generate_vendor_code(name, session)
            vendor.vendor_code = code
            session.flush()
    except (IntegrityError, ValueError):
        # A concurrent onboarding of the SAME new vendor won the race (its
        # unique name committed first). Reuse it instead of creating a
        # duplicate / a _2 code. If the name genuinely still isn't there, the
        # error was something else -- re-raise it.
        existing = vendor_service.get_vendor_by_name(name, session)
        if existing is None:
            raise
        logger.info(
            "Concurrent vendor onboarding race for '%s' -- reusing existing vendor "
            "(id=%s, code=%s); no duplicate created.",
            name,
            existing.id,
            existing.vendor_code,
        )
        return existing, None

    logger.info("New vendor onboarded: '%s' (id=%s, code=%s).", vendor.name, vendor.id, code)
    return vendor, f"New vendor '{vendor.name}' onboarded with code {code}."


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
        # No code-shaped prefix at all -- reuse the existing vendor for this
        # company name, or onboard a new one (race-safe: no duplicate/_2 code).
        name = _vendor_name_from_filename(file_path.name)
        vendor, onboarding_message = _resolve_or_onboard_vendor(name, session)

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
        # Google Sheets sync deliberately does NOT run here any more: this
        # function executes inside `processor.py`'s `session.begin_nested()`,
        # and a network call there holds a DB transaction open for the whole
        # request (measured at ~20s per import while the OAuth token lacks the
        # spreadsheets scope). It is now triggered by `processor.py` AFTER the
        # transaction closes -- same behaviour, bounded, transaction-safe.
        pass

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


def _customer_name_from_filename(filename: str) -> str:
    return Path(filename).stem.strip()


def _resolve_or_onboard_customer(name: str, session: Session) -> tuple["Customer", str | None]:
    """Reuse the existing customer for this NAME, or onboard a brand-new one
    (create + generate ONE permanent Customer Code). Race-safe, mirroring
    `_resolve_or_onboard_vendor` exactly: if two order files for the same new
    customer are processed almost simultaneously, `ux_customers_name_lower`
    makes the second INSERT fail; we recover inside a SAVEPOINT and reuse the
    customer the winning transaction created -- only ONE customer / ONE code
    ever exists and both imports share it."""
    existing = customer_service.get_customer_by_name(name, session)
    if existing is not None:
        return existing, None  # reuse existing customer + code, unchanged

    try:
        with session.begin_nested():  # SAVEPOINT: undoable if the race is lost
            customer = customer_service.create_customer(name, session)
            code = customer_code_service.generate_customer_code(name, session)
            customer.customer_code = code
            session.flush()
    except (IntegrityError, ValueError):
        existing = customer_service.get_customer_by_name(name, session)
        if existing is None:
            raise
        logger.info(
            "Concurrent customer onboarding race for '%s' -- reusing existing customer "
            "(id=%s, code=%s); no duplicate created.",
            name,
            existing.id,
            existing.customer_code,
        )
        return existing, None

    logger.info("New customer onboarded: '%s' (id=%s, code=%s)", customer.name, customer.id, code)
    return customer, f"New customer '{customer.name}' onboarded with code {code}."


def _dispatch_customer_order(
    file_path: Path, classification: Classification, session: Session
) -> DispatchResult:
    customer = None
    onboarding_message: str | None = None

    if classification.customer_id is not None:
        customer = customer_service.get_customer(classification.customer_id, session)
        if customer is None:
            raise ValueError(f"Customer {classification.customer_id} does not exist.")
    elif classification.customer_code is not None:
        # A code-shaped prefix was found in the filename but didn't match any
        # customer -- reject rather than silently misassigning the order.
        raise UnknownCustomerCodeError(classification.customer_code)
    elif classification.resolve_customer:
        # WhatsApp Customer Order with no code-shaped prefix -- first-time
        # onboarding by name, exactly like an unrecognized Vendor Inventory
        # filename onboards a new vendor.
        name = _customer_name_from_filename(file_path.name)
        customer, onboarding_message = _resolve_or_onboard_customer(name, session)
    # else: Gmail/manual Customer Order -- customer identification was never
    # attempted (see `Classification.resolve_customer`), so `customer` stays
    # None exactly as before this feature existed.

    try:
        result = order_service.run_customer_order_import(
            file_path, session, customer_id=customer.id if customer is not None else None
        )
    except order_service.DuplicateCustomerOrderFileError as exc:
        raise DocumentAlreadyProcessedError(exc.existing_order_id, str(exc)) from exc
    except order_service.CustomerOrderQuantityMissingError as exc:
        # A real line-item table was found but has no quantity column -- do not
        # invent one. Report NEEDS_REVIEW (no order persisted) rather than fail.
        return DispatchResult(
            message=str(exc),
            customer_id=customer.id if customer is not None else None,
            customer_name=customer.name if customer is not None else None,
            core_status="NEEDS_REVIEW",
        )

    return DispatchResult(
        row_count=result.row_count,
        error_count=result.error_count,
        message=onboarding_message,
        customer_order_id=result.order_id,
        customer_id=customer.id if customer is not None else None,
        customer_name=customer.name if customer is not None else None,
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
        return _dispatch_customer_order(file_path, classification, session)
    if classification.document_type == IncomingDocumentType.DELIVERY:
        return _dispatch_delivery(file_path, session)
    if classification.document_type == IncomingDocumentType.VENDOR_INVOICE:
        return _dispatch_vendor_invoice(file_path, session)
    raise NotImplementedYetError(f"No dispatcher for document type {classification.document_type}.")
