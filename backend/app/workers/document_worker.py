"""Runs after the WhatsApp webhook has already returned its ack response
(scheduled via FastAPI `BackgroundTasks` -- see
`backend/app/api/routes/whatsapp.py`). Opens its own DB session via
`core.db.get_session()` rather than the request-scoped `Depends(get_db)`,
since this genuinely runs after the request that triggered it has already
completed."""

from __future__ import annotations

from backend.app.documents import service as documents_service
from backend.app.documents.models import DocumentSource
from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.media import download_document_media
from backend.app.integrations.whatsapp.parser import IncomingWhatsAppMessage
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.processor import process_document
from core.db import get_session
from core.logging_setup import get_logger

logger = get_logger(__name__)


def handle_incoming_whatsapp_message(message: IncomingWhatsAppMessage) -> None:
    # Step 1: entering the pipeline.
    logger.info(
        "WhatsApp pipeline step 1: entering handle_incoming_whatsapp_message "
        "(sender=%s, filename=%s, media_id=%s, message_id=%s)",
        message.sender,
        message.filename,
        message.media_id,
        message.message_id,
    )

    try:
        client = WhatsAppClient(whatsapp_settings)
        file_path = download_document_media(message.media_id, message.filename, client)
    except Exception:
        # Step 11: any download failure, with full traceback.
        logger.exception(
            "WhatsApp pipeline: FAILED downloading media %s from %s",
            message.media_id,
            message.sender,
        )
        with get_session() as session:
            document = documents_service.record_received(
                DocumentSource.WHATSAPP,
                message.filename,
                session,
                sender=message.sender,
                whatsapp_message_id=message.message_id,
            )
            if document.status.value == "RECEIVED":
                documents_service.mark_download_failed(
                    document, "Could not download this attachment from WhatsApp.", session
                )
        return

    # Media downloaded + staged; next we open a DB session and process it.
    logger.info(
        "WhatsApp pipeline: media staged at %s -- opening DB session "
        "(note: get_session() runs Base.metadata.create_all against the configured "
        "database) and starting document processing...",
        file_path,
    )
    try:
        with get_session() as session:
            logger.info("WhatsApp pipeline: DB session opened; calling process_document...")
            metadata = DocumentMetadata(
                sender=message.sender,
                caption=message.caption,
                external_message_id=message.message_id,
                original_filename=message.filename,
            )
            result = process_document(DocumentSource.WHATSAPP, file_path, metadata, session)
            logger.info(
                "WhatsApp pipeline: process_document finished for %s "
                "(document_id=%s, status=%s, type=%s)",
                message.filename,
                getattr(result, "document_id", None),
                getattr(getattr(result, "status", None), "value", None),
                getattr(getattr(result, "document_type", None), "value", None),
            )
    except Exception:
        # Step 11: any processing/DB failure, with full traceback. Re-raised so
        # behaviour is unchanged -- only observability is added.
        logger.exception(
            "WhatsApp pipeline: FAILED during DB session / process_document for %s (media_id=%s)",
            message.filename,
            message.media_id,
        )
        raise
