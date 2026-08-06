"""Upload orchestration for the manual Vendor Invoice upload page. Delegates
every file to the Document Processing Engine -- this module's only
remaining job is translating between the route's `UploadFile` list and the
engine's generic `ProcessingResult`, mirroring
`app.services.delivery_service`'s shape exactly."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import UploadFile
from sqlalchemy.orm import Session

from backend.app.documents.models import DocumentSource, IncomingDocumentType
from backend.app.services.document_processor import staging
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.processor import ProcessingResult, process_document
from core.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class VendorInvoiceUploadOutcome:
    file_name: str
    invoice_import_id: int | None
    status: str
    vendor_id: int | None
    vendor_name: str | None
    row_count: int
    error_count: int
    message: str | None = None


def _resolve_status(result: ProcessingResult) -> str:
    if result.is_duplicate:
        return "SKIPPED_DUPLICATE"
    return result.core_status or "FAILED"


def _to_outcome(file_name: str, result: ProcessingResult) -> VendorInvoiceUploadOutcome:
    return VendorInvoiceUploadOutcome(
        file_name=file_name,
        invoice_import_id=result.invoice_verification_id,
        status=_resolve_status(result),
        vendor_id=result.vendor_id,
        vendor_name=result.vendor_name,
        row_count=result.row_count,
        error_count=result.error_count,
        message=result.message,
    )


def process_invoice_uploads(
    files: list[UploadFile], session: Session, *, sender: str | None = None
) -> list[VendorInvoiceUploadOutcome]:
    outcomes: list[VendorInvoiceUploadOutcome] = []

    for upload in files:
        file_name = upload.filename or "upload"
        try:
            saved_path = staging.save_incoming_upload(upload, DocumentSource.MANUAL)
            metadata = DocumentMetadata(
                sender=sender,
                document_type_hint=IncomingDocumentType.VENDOR_INVOICE,
                original_filename=file_name,
            )
            result = process_document(DocumentSource.MANUAL, saved_path, metadata, session)
            outcomes.append(_to_outcome(file_name, result))
        except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
            logger.exception("Failed to verify invoice file '%s'", file_name)
            outcomes.append(
                VendorInvoiceUploadOutcome(
                    file_name=file_name,
                    invoice_import_id=None,
                    status="FAILED",
                    vendor_id=None,
                    vendor_name=None,
                    row_count=0,
                    error_count=0,
                    message=str(exc),
                )
            )
        finally:
            upload.file.close()

    return outcomes
