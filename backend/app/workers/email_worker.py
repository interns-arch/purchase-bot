"""Gmail Customer Order Automation: polls the configured mailbox for unread
messages, downloads Excel attachments, and hands each off to the same
Document Processing Engine the manual Customer Orders upload uses --
`process_document(..., document_type_hint=CUSTOMER_ORDER)` -- so this
worker never reimplements `core.services.customer_order_service`.

Scheduled periodically by `backend/app/workers/scheduler.py`; opens its own
DB session via `core.db.get_session()` rather than the request-scoped
`Depends(get_db)`, the same pattern
`backend/app/workers/document_worker.py` uses for the WhatsApp path, since
this also runs outside any HTTP request."""

from __future__ import annotations

from pathlib import Path

from backend.app.documents import service as documents_service
from backend.app.documents.models import DocumentSource, IncomingDocumentType
from backend.app.integrations.gmail import status_service
from backend.app.integrations.gmail.client import (
    GmailNotConfiguredError,
    IncomingEmailMessage,
    get_gmail_client,
)
from backend.app.integrations.gmail.config import gmail_settings
from backend.app.services.document_processor import staging
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.processor import process_document
from core.db import get_session
from core.logging_setup import get_logger

logger = get_logger(__name__)

_EXCEL_EXTENSIONS = {".xlsx", ".xls"}


def _excel_attachments(message: IncomingEmailMessage) -> list:
    excel_only = [
        attachment
        for attachment in message.attachments
        if Path(attachment.filename).suffix.lower() in _EXCEL_EXTENSIONS
    ]

    # Business rule: when multiple attachments are present, the trailing
    # ones are typically signature images/letterhead, not the order sheet
    # itself -- drop the last two.
    if len(excel_only) > gmail_settings.max_attachments_before_trim:
        return excel_only[: -gmail_settings.max_attachments_before_trim]
    return excel_only


def _process_message(message: IncomingEmailMessage) -> None:
    attachments = _excel_attachments(message)
    if not attachments:
        logger.info(
            "Gmail message from %s (%s) has no usable Excel attachment after filtering -- skipping.",
            message.sender,
            message.subject,
        )
        return

    with get_session() as session:
        if documents_service.find_by_email_message_id(message.message_id, session) is not None:
            logger.info("Gmail message %s already processed -- skipping.", message.message_id)
            return

    for attachment in attachments:
        file_path = staging.save_incoming_bytes(
            attachment.content, attachment.filename, DocumentSource.EMAIL
        )
        with get_session() as session:
            metadata = DocumentMetadata(
                sender=message.sender,
                document_type_hint=IncomingDocumentType.CUSTOMER_ORDER,
                external_message_id=message.message_id,
                original_filename=attachment.filename,
            )
            process_document(DocumentSource.EMAIL, file_path, metadata, session)


def poll_gmail_inbox() -> None:
    """Entry point called by the scheduler. Never raises -- any failure
    (missing credentials, a transient IMAP/API error) is logged and
    recorded on the Gmail integration status row instead of crashing the
    scheduler thread."""
    if not gmail_settings.enabled:
        return

    try:
        client = get_gmail_client(gmail_settings)
    except GmailNotConfiguredError as exc:
        logger.error("Gmail automation is enabled but not configured: %s", exc)
        with get_session() as session:
            status_service.record_poll(session, success=False, message=str(exc))
        return

    try:
        messages = client.fetch_unread_messages()
    except Exception as exc:  # noqa: BLE001 -- a poll failure must not crash the scheduler
        logger.exception("Gmail poll failed.")
        with get_session() as session:
            status_service.record_poll(session, success=False, message=str(exc))
        return

    for message in messages:
        try:
            _process_message(message)
            client.mark_processed(message)
            with get_session() as session:
                status_service.record_message_processed(session)
        except Exception:  # noqa: BLE001 -- one bad message must not block the rest of the poll
            logger.exception("Failed to process Gmail message %s", message.message_id)

    with get_session() as session:
        status_service.record_poll(
            session, success=True, message=f"Processed {len(messages)} unread message(s)."
        )
