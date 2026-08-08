"""Runs after the WhatsApp webhook has already returned its ack response
(scheduled via FastAPI `BackgroundTasks` -- see
`backend/app/api/routes/whatsapp.py`). Opens its own DB session via
`core.db.get_session()` rather than the request-scoped `Depends(get_db)`,
since this genuinely runs after the request that triggered it has already
completed.

Command-routing layer (added for "Customer Orders over WhatsApp"): because
both Vendor Inventory and Customer Order files can now arrive over WhatsApp,
a file is only imported after the sender has first sent a text command
(`Vendor` / `Customer` -- see `commands.py`). The command is remembered
per-number (`command_store`), used to pick which existing import workflow to
run, and cleared once that file has been processed. This module only routes;
it never reimplements any import logic -- the Vendor Inventory and Customer
Order imports are reached through the unchanged `process_document`."""

from __future__ import annotations

from backend.app.documents import service as documents_service
from backend.app.documents.models import DocumentSource
from backend.app.integrations.whatsapp import command_store, commands
from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.integrations.whatsapp.commands import WhatsAppCommand
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.media import download_document_media
from backend.app.integrations.whatsapp.outbound import send_reply_safe
from backend.app.integrations.whatsapp.parser import (
    IncomingWhatsAppMessage,
    IncomingWhatsAppText,
)
from backend.app.notifications import emitters as notifications
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.processor import process_document
from core.db import get_session
from core.logging_setup import get_logger

logger = get_logger(__name__)


def handle_incoming_whatsapp_text(message: IncomingWhatsAppText) -> None:
    """A plain text message. If it's a known routing command, remember it for
    this number so the next file is imported accordingly; otherwise reply
    with the instruction (requirement 6)."""
    command = commands.parse_command(message.text)
    if command is None:
        logger.info(
            "WhatsApp text from %s is not a routing command (%r) -- replying with instructions.",
            message.sender,
            message.text,
        )
        send_reply_safe(message.sender, commands.instruction_text())
        return

    with get_session() as session:
        command_store.set_command(message.sender, command.key, session)
    logger.info("WhatsApp routing command from %s stored: %s", message.sender, command.key)
    send_reply_safe(
        message.sender,
        f"Got it — now upload your {command.label} file (Excel).",
    )


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

    # Routing: which import to run is decided by this sender's last text
    # command. No command -> do not import; reply with the instruction
    # (requirement 5).
    with get_session() as session:
        command_key = command_store.get_command(message.sender, session)
    command = commands.get_command(command_key)
    if command is None:
        logger.info(
            "WhatsApp file from %s has no pending routing command -- not importing; "
            "replying with instructions.",
            message.sender,
        )
        send_reply_safe(message.sender, commands.instruction_text())
        return

    logger.info(
        "WhatsApp file from %s routed by command '%s' -> document_type=%s",
        message.sender,
        command.key,
        command.document_type.value,
    )
    try:
        _download_and_process(message, command)
    finally:
        # Requirement 4: clear the stored command once the file has been
        # processed -- whether it succeeded or failed -- so the next file
        # requires a fresh command.
        with get_session() as session:
            command_store.clear_command(message.sender, session)
        logger.info("Cleared pending WhatsApp command for %s after processing.", message.sender)


def _download_and_process(message: IncomingWhatsAppMessage, command: WhatsAppCommand) -> None:
    """Download the media and run it through the existing import workflow the
    resolved `command` maps to. Unchanged from the original single-workflow
    path except that the command-derived `document_type` is passed as a hint
    (honoured by `detector.classify` for WhatsApp)."""
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
        notifications.publish_download_failure(
            "WhatsApp", message.filename, "Could not download this attachment from WhatsApp."
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
                document_type_hint=command.document_type,
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
            notifications.publish_document_result("WhatsApp", result)
    except Exception:
        # Step 11: any processing/DB failure, with full traceback. Re-raised so
        # behaviour is unchanged -- only observability is added. The caller's
        # `finally` still clears the pending command (requirement 4).
        logger.exception(
            "WhatsApp pipeline: FAILED during DB session / process_document for %s (media_id=%s)",
            message.filename,
            message.media_id,
        )
        raise
