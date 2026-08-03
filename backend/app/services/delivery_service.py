"""Upload orchestration for Delivery Upload. Delegates every file to the
Document Processing Engine -- this module's only remaining job is
translating between the route's `UploadFile` list and the engine's generic
`ProcessingResult`, preserving the exact `DeliveryUploadOutcome`
shape/status strings the route already expects."""

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
class DeliveryUploadOutcome:
    file_name: str
    import_id: int | None
    status: str
    row_count: int
    error_count: int
    message: str | None = None


def _resolve_status(result: ProcessingResult) -> str:
    if result.is_duplicate:
        return "SKIPPED_DUPLICATE"
    return result.core_status or "FAILED"


def _to_outcome(file_name: str, result: ProcessingResult) -> DeliveryUploadOutcome:
    return DeliveryUploadOutcome(
        file_name=file_name,
        import_id=result.delivery_import_id,
        status=_resolve_status(result),
        row_count=result.row_count,
        error_count=result.error_count,
        message=result.message,
    )


def process_delivery_uploads(
    files: list[UploadFile], session: Session, *, sender: str | None = None
) -> list[DeliveryUploadOutcome]:
    outcomes: list[DeliveryUploadOutcome] = []

    for upload in files:
        file_name = upload.filename or "upload"
        try:
            saved_path = staging.save_incoming_upload(upload, DocumentSource.MANUAL)
            metadata = DocumentMetadata(
                sender=sender,
                document_type_hint=IncomingDocumentType.DELIVERY,
                original_filename=file_name,
            )
            result = process_document(DocumentSource.MANUAL, saved_path, metadata, session)
            outcomes.append(_to_outcome(file_name, result))
        except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
            logger.exception("Failed to import delivery file '%s'", file_name)
            outcomes.append(
                DeliveryUploadOutcome(
                    file_name=file_name,
                    import_id=None,
                    status="FAILED",
                    row_count=0,
                    error_count=0,
                    message=str(exc),
                )
            )
        finally:
            upload.file.close()

    return outcomes
