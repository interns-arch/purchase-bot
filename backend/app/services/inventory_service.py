"""Upload orchestration for Inventory Import. Delegates every file to the
Document Processing Engine (`backend/app/services/document_processor/`) --
this module's only remaining job is translating between the route's
`UploadFile` list and the engine's generic `ProcessingResult`, preserving
the exact `UploadOutcome` shape/status strings the route and frontend
already expect (this refactor changes no external behavior)."""

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
class UploadOutcome:
    file_name: str
    vendor_id: int | None
    vendor_name: str | None
    import_id: int | None
    status: str
    is_duplicate: bool
    row_count: int
    error_count: int
    message: str | None
    error: str | None = None


def _resolve_status(result: ProcessingResult) -> str:
    if result.is_duplicate:
        return "SKIPPED_DUPLICATE"
    return result.core_status or "FAILED"


def _to_outcome(file_name: str, result: ProcessingResult) -> UploadOutcome:
    status = _resolve_status(result)

    if result.vendor_id is None:
        # Vendor resolution never ran -- a pre-classification validation
        # failure (bad extension, empty file). The route's response schema
        # requires non-null vendor_id/vendor_name/import_id, so this must go
        # through its `error`-branch fallback (`or 0`/`or "-"`), the same
        # path an unexpected exception below takes.
        return UploadOutcome(
            file_name=file_name,
            vendor_id=None,
            vendor_name=None,
            import_id=None,
            status=status,
            is_duplicate=False,
            row_count=0,
            error_count=0,
            message=None,
            error=result.message or "Failed to process this file.",
        )

    return UploadOutcome(
        file_name=file_name,
        vendor_id=result.vendor_id,
        vendor_name=result.vendor_name,
        import_id=result.inventory_import_id,
        status=status,
        is_duplicate=result.is_duplicate,
        row_count=result.row_count,
        error_count=result.error_count,
        message=result.message,
    )


def process_uploads(
    files: list[UploadFile], session: Session, *, sender: str | None = None
) -> list[UploadOutcome]:
    """Import every uploaded file. There is no manual vendor selection --
    each file's vendor is always derived from its own filename, auto-created
    on first sight (see `document_processor.dispatcher._get_or_create_vendor`)."""
    outcomes: list[UploadOutcome] = []

    for upload in files:
        file_name = upload.filename or "upload"
        try:
            saved_path = staging.save_incoming_upload(upload, DocumentSource.MANUAL)
            metadata = DocumentMetadata(
                sender=sender,
                document_type_hint=IncomingDocumentType.VENDOR_INVENTORY,
                original_filename=file_name,
            )
            result = process_document(DocumentSource.MANUAL, saved_path, metadata, session)
            outcomes.append(_to_outcome(file_name, result))
        except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the batch
            logger.exception("Failed to import uploaded file '%s'", file_name)
            outcomes.append(
                UploadOutcome(
                    file_name=file_name,
                    vendor_id=None,
                    vendor_name=None,
                    import_id=None,
                    status="FAILED",
                    is_duplicate=False,
                    row_count=0,
                    error_count=0,
                    message=None,
                    error=str(exc),
                )
            )
        finally:
            upload.file.close()

    return outcomes
