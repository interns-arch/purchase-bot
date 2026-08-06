"""The single public gateway into business logic: `process_document(source,
file_path, metadata, session)`. Callers (manual-upload glue, the WhatsApp
worker) never call `core.services.*` directly -- they only ever call this,
so the business logic never knows where a file came from."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from backend.app.documents import service as documents_service
from backend.app.documents.models import (
    DocumentSource,
    IncomingDocumentStatus,
    IncomingDocumentType,
)
from backend.app.services.document_processor import staging
from backend.app.services.document_processor.detector import classify
from backend.app.services.document_processor.dispatcher import (
    DocumentAlreadyProcessedError,
    NotImplementedYetError,
    dispatch,
)
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.validator import (
    DocumentValidationError,
    validate_file,
)
from core.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class ProcessingResult:
    document_id: int
    status: IncomingDocumentStatus
    document_type: IncomingDocumentType
    file_name: str
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
    core_status: str | None = None


def _early_result(document, message: str | None = None, *, is_duplicate: bool = False) -> ProcessingResult:
    return ProcessingResult(
        document_id=document.id,
        status=document.status,
        document_type=document.document_type,
        file_name=document.filename,
        message=message if message is not None else document.error_message,
        is_duplicate=is_duplicate,
    )


def process_document(
    source: DocumentSource, file_path: Path, metadata: DocumentMetadata, session: Session
) -> ProcessingResult:
    document = documents_service.record_received(
        source,
        metadata.original_filename or file_path.name,
        session,
        sender=metadata.sender,
        whatsapp_message_id=metadata.external_message_id if source == DocumentSource.WHATSAPP else None,
        email_message_id=metadata.external_message_id if source == DocumentSource.EMAIL else None,
    )

    # A redelivered WhatsApp message: `record_received` returned the
    # already-processed row untouched -- nothing new to do.
    if document.status != IncomingDocumentStatus.RECEIVED:
        return _early_result(
            document, is_duplicate=document.status == IncomingDocumentStatus.SKIPPED_DUPLICATE
        )

    try:
        validate_file(file_path)
    except DocumentValidationError as exc:
        documents_service.mark_failed(document, str(exc), session)
        staging.mark_failed_location(file_path)
        return _early_result(document, str(exc))

    # Step 7: before document classification.
    logger.info(
        "process_document step 7: classifying document '%s' (source=%s)",
        file_path.name,
        source.value,
    )
    classification = classify(file_path, metadata, session)
    # Step 8: after classification.
    logger.info(
        "process_document step 8: classified '%s' as %s",
        file_path.name,
        classification.document_type.value,
    )
    documents_service.set_document_type(document, classification.document_type, session)

    if classification.document_type == IncomingDocumentType.UNKNOWN:
        documents_service.mark_needs_review(document, session)
        staging.mark_failed_location(file_path)
        return _early_result(document)

    try:
        # Step 9: before dispatching to the type-specific importer (e.g.
        # inventory import) -- this is the DB-heavy business-logic step.
        logger.info(
            "process_document step 9: dispatching '%s' to %s import...",
            file_path.name,
            classification.document_type.value,
        )
        with session.begin_nested():
            result = dispatch(file_path, classification, session)
        # Step 10: after processing returned successfully.
        logger.info(
            "process_document step 10: %s import finished for '%s' "
            "(rows=%s, errors=%s, duplicate=%s, core_status=%s)",
            classification.document_type.value,
            file_path.name,
            result.row_count,
            result.error_count,
            result.is_duplicate,
            result.core_status,
        )
    except DocumentAlreadyProcessedError as exc:
        documents_service.mark_skipped_duplicate(document, str(exc), session)
        staging.mark_processed_location(file_path)
        # `existing_reference_id` is generic on the exception -- which field
        # it belongs to depends on which document type raised it (only
        # CUSTOMER_ORDER and DELIVERY ever raise this; VENDOR_INVENTORY's
        # duplicate path is return-based, handled below via `is_duplicate`).
        reference_kwargs: dict[str, int] = {}
        if classification.document_type == IncomingDocumentType.CUSTOMER_ORDER:
            reference_kwargs["customer_order_id"] = exc.existing_reference_id
        elif classification.document_type == IncomingDocumentType.DELIVERY:
            reference_kwargs["delivery_import_id"] = exc.existing_reference_id
        return ProcessingResult(
            document_id=document.id,
            status=document.status,
            document_type=document.document_type,
            file_name=document.filename,
            message=str(exc),
            is_duplicate=True,
            **reference_kwargs,
        )
    except NotImplementedYetError as exc:
        documents_service.mark_unsupported(document, str(exc), session)
        staging.mark_failed_location(file_path)
        return _early_result(document, str(exc))
    except Exception as exc:  # noqa: BLE001 -- one bad document must not crash the caller
        logger.exception("Failed to process document '%s'", file_path.name)
        documents_service.mark_failed(document, str(exc), session)
        staging.mark_failed_location(file_path)
        return _early_result(document, str(exc))

    if result.is_duplicate:
        documents_service.mark_skipped_duplicate(document, result.message or "Duplicate.", session)
        staging.mark_processed_location(file_path)
        return ProcessingResult(
            document_id=document.id,
            status=document.status,
            document_type=document.document_type,
            file_name=document.filename,
            message=result.message,
            vendor_id=result.vendor_id,
            vendor_name=result.vendor_name,
            inventory_import_id=result.inventory_import_id,
            invoice_verification_id=result.invoice_verification_id,
            is_duplicate=True,
            core_status=result.core_status,
        )

    # The underlying core service can itself report FAILED via a normal
    # return (e.g. zero valid rows) rather than an exception -- treat that
    # the same as any other failure for the Document Inbox's own status,
    # even though nothing was raised here. NEEDS_REVIEW (Vendor Invoice
    # Verification only, e.g. unreadable PDF or unresolved vendor) gets its
    # own status too, rather than being silently reported as a clean
    # PROCESSED with zero errors.
    if result.core_status == "FAILED":
        documents_service.mark_failed(
            document, result.message or "Import failed -- no valid rows.", session
        )
        staging.mark_failed_location(file_path)
    elif result.core_status == "NEEDS_REVIEW":
        documents_service.mark_needs_review(document, session, result.message)
        staging.mark_failed_location(file_path)
    else:
        documents_service.mark_processed(
            document,
            session,
            has_errors=result.error_count > 0,
            inventory_import_id=result.inventory_import_id,
            customer_order_id=result.customer_order_id,
            delivery_import_id=result.delivery_import_id,
            invoice_verification_id=result.invoice_verification_id,
        )
        staging.mark_processed_location(file_path)

    return ProcessingResult(
        document_id=document.id,
        status=document.status,
        document_type=document.document_type,
        file_name=document.filename,
        row_count=result.row_count,
        error_count=result.error_count,
        message=result.message,
        vendor_id=result.vendor_id,
        vendor_name=result.vendor_name,
        inventory_import_id=result.inventory_import_id,
        customer_order_id=result.customer_order_id,
        delivery_import_id=result.delivery_import_id,
        invoice_verification_id=result.invoice_verification_id,
        core_status=result.core_status,
    )
