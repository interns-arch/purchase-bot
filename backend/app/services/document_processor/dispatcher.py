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
from backend.app.services.document_processor.detector import Classification
from core.logging_setup import get_logger
from core.models import ImportStatus
from core.services import customer_order_service as order_service
from core.services import inventory_import_service as import_service
from core.services import vendor_delivery_service
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
    is_duplicate: bool = False
    # The underlying `core.services.*` result's own status string (e.g.
    # "COMPLETED", "COMPLETED_WITH_ERRORS", or "FAILED" for zero-valid-rows)
    # -- callers that need the exact original status string (the manual
    # upload glue, to keep its response byte-identical to before this
    # refactor) read this instead of re-deriving it from row/error counts.
    core_status: str | None = None


def _vendor_name_from_filename(filename: str) -> str:
    return Path(filename).stem.strip()


def _get_or_create_vendor(name: str, session: Session):
    vendor = vendor_service.get_vendor_by_name(name, session)
    if vendor is None:
        vendor = vendor_service.create_vendor(name, session)
        logger.info("New vendor created from upload: '%s' (id=%s)", vendor.name, vendor.id)
    return vendor


def _dispatch_inventory(
    file_path: Path, classification: Classification, session: Session
) -> DispatchResult:
    if classification.vendor_id is not None:
        vendor = vendor_service.get_vendor(classification.vendor_id, session)
        if vendor is None:
            raise ValueError(f"Vendor {classification.vendor_id} does not exist.")
    else:
        vendor = _get_or_create_vendor(_vendor_name_from_filename(file_path.name), session)

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

    return DispatchResult(
        row_count=result.row_count,
        error_count=result.error_count,
        message=result.message,
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


def dispatch(file_path: Path, classification: Classification, session: Session) -> DispatchResult:
    if classification.document_type == IncomingDocumentType.VENDOR_INVENTORY:
        return _dispatch_inventory(file_path, classification, session)
    if classification.document_type == IncomingDocumentType.CUSTOMER_ORDER:
        return _dispatch_customer_order(file_path, session)
    if classification.document_type == IncomingDocumentType.DELIVERY:
        return _dispatch_delivery(file_path, session)
    if classification.document_type == IncomingDocumentType.VENDOR_INVOICE:
        raise NotImplementedYetError(
            "Vendor Invoice PDF processing is not implemented yet -- no PDF-to-structured-data "
            "parser exists. This document has been recorded for visibility only."
        )
    raise NotImplementedYetError(f"No dispatcher for document type {classification.document_type}.")
