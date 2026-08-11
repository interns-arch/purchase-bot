"""Gmail inbox automation: polls the configured mailbox for unread messages,
downloads the usable attachments (Excel customer orders + PDF purchase
bills), and hands each off to the same Document Processing Engine every
other upload path uses. The document type is forced by source in
`detector.classify` (EMAIL spreadsheet -> Customer Order, EMAIL PDF -> Vendor
Invoice), so this worker never reimplements `core.services.*`.

Scheduled periodically by `backend/app/workers/scheduler.py`; opens its own
DB session via `core.db.get_session()` rather than the request-scoped
`Depends(get_db)`, the same pattern
`backend/app/workers/document_worker.py` uses for the WhatsApp path, since
this also runs outside any HTTP request."""

from __future__ import annotations

from pathlib import Path

from backend.app.documents import service as documents_service
from backend.app.documents.models import DocumentSource
from backend.app.integrations.gmail import status_service
from backend.app.integrations.gmail.client import (
    GmailNotConfiguredError,
    IncomingEmailMessage,
    get_gmail_client,
)
from backend.app.integrations.gmail.config import gmail_settings
from backend.app.integrations.whatsapp import allocation_batch
from backend.app.notifications import emitters as notifications
from backend.app.services.document_processor import staging
from backend.app.services.document_processor.metadata import DocumentMetadata
from backend.app.services.document_processor.processor import process_document
from core.db import get_session
from core.logging_setup import get_logger

logger = get_logger(__name__)

_EXCEL_EXTENSIONS = {".xlsx", ".xls"}
_PDF_EXTENSIONS = {".pdf"}


def _usable_attachments(message: IncomingEmailMessage) -> list:
    """Excel attachments (customer orders) and PDF attachments (vendor
    purchase bills), optionally narrowed to a file-name prefix.

    With GMAIL_ATTACHMENT_PREFIX set (e.g. "purchase_order"), ONLY attachments
    whose name starts with that prefix are extracted -- one match is taken
    alone, several matches are ALL taken -- and the trailing-attachment trim
    heuristic below is skipped entirely (the prefix already excludes the
    signature/letterhead junk that heuristic guards against, and it must
    never drop a genuine purchase_order file)."""
    prefix = gmail_settings.attachment_prefix
    if prefix:
        matching = [
            attachment
            for attachment in message.attachments
            if attachment.filename
            and attachment.filename.strip().lower().startswith(prefix)
            and Path(attachment.filename).suffix.lower() in (_EXCEL_EXTENSIONS | _PDF_EXTENSIONS)
        ]
        skipped = len(message.attachments) - len(matching)
        if skipped:
            logger.info(
                "Gmail message from %s: %d attachment(s) ignored (name does not "
                "start with %r); %d extracted.",
                message.sender,
                skipped,
                prefix,
                len(matching),
            )
        return matching

    excel_only = [
        attachment
        for attachment in message.attachments
        if Path(attachment.filename).suffix.lower() in _EXCEL_EXTENSIONS
    ]
    pdf_only = [
        attachment
        for attachment in message.attachments
        if Path(attachment.filename).suffix.lower() in _PDF_EXTENSIONS
    ]

    # Business rule: when multiple Excel attachments are present, the trailing
    # ones are typically signature images/letterhead, not the order sheet
    # itself -- drop the last two.
    if len(excel_only) > gmail_settings.max_attachments_before_trim:
        excel_only = excel_only[: -gmail_settings.max_attachments_before_trim]

    return excel_only + pdf_only


def _saved_name_for(original_filename: str) -> str:
    """The name an extracted attachment is imported under. With
    GMAIL_SAVE_ATTACHMENT_AS set, every attachment gets that fixed name (the
    original's extension is appended when the configured name has none, so a
    .xlsx can never become extensionless); otherwise the original name is
    kept."""
    configured = gmail_settings.save_attachment_as
    if not configured:
        return original_filename
    if not Path(configured).suffix:
        return configured + Path(original_filename).suffix.lower()
    return configured


def _process_message(message: IncomingEmailMessage) -> None:
    attachments = _usable_attachments(message)
    if not attachments:
        logger.info(
            "Gmail message from %s (%s) has no usable Excel/PDF attachment after filtering -- skipping.",
            message.sender,
            message.subject,
        )
        return

    with get_session() as session:
        if documents_service.find_by_email_message_id(message.message_id, session) is not None:
            logger.info("Gmail message %s already processed -- skipping.", message.message_id)
            return

    for attachment in attachments:
        saved_name = _saved_name_for(attachment.filename)
        if saved_name != attachment.filename:
            logger.info(
                "Gmail attachment %r will be imported as %r (GMAIL_SAVE_ATTACHMENT_AS).",
                attachment.filename,
                saved_name,
            )
        file_path = staging.save_incoming_bytes(
            attachment.content, saved_name, DocumentSource.EMAIL
        )
        with get_session() as session:
            # No document_type_hint: EMAIL documents are routed by source +
            # file format in detector.classify (spreadsheet -> Customer Order,
            # PDF -> Vendor Invoice), so a hint here would be ignored and
            # misleading.
            metadata = DocumentMetadata(
                sender=message.sender,
                external_message_id=message.message_id,
                original_filename=saved_name,
            )
            result = process_document(DocumentSource.EMAIL, file_path, metadata, session)
        # The session above has now committed -- announce the result only
        # after the transaction is durable, never before.
        notifications.publish_document_result("Gmail", result)

        # Founder automation ("Combined ZIP" mode): queue a successfully
        # imported customer order for automatic vendor selection -- same
        # batch/debounce as WhatsApp-sourced orders.
        order_id = _successful_customer_order_id(result)
        if order_id is not None:
            allocation_batch.request_order_allocation(order_id)


def _successful_customer_order_id(result) -> int | None:
    doc_type = getattr(getattr(result, "document_type", None), "value", None)
    status = getattr(getattr(result, "status", None), "value", None)
    if doc_type == "CUSTOMER_ORDER" and status in ("PROCESSED", "PROCESSED_WITH_ERRORS"):
        return getattr(result, "customer_order_id", None)
    return None


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
