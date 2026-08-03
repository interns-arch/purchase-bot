"""CRUD for `IncomingDocument`, used by the Document Processing Engine
(`backend/app/services/document_processor/`) to record every file's
lifecycle, and by the Document Inbox API to list them. Pure persistence --
no classification/dispatch logic here."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.documents.models import (
    DocumentSource,
    IncomingDocument,
    IncomingDocumentStatus,
    IncomingDocumentType,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def find_by_whatsapp_message_id(message_id: str, session: Session) -> IncomingDocument | None:
    return session.execute(
        select(IncomingDocument).where(IncomingDocument.whatsapp_message_id == message_id)
    ).scalar_one_or_none()


def record_received(
    source: DocumentSource,
    filename: str,
    session: Session,
    *,
    sender: str | None = None,
    whatsapp_message_id: str | None = None,
) -> IncomingDocument:
    """Creates the initial `RECEIVED` row. If `whatsapp_message_id` is given
    and already recorded, returns the existing row instead of creating a
    duplicate -- Meta's webhook delivery is at-least-once, so a retried POST
    must be a safe no-op rather than reprocessing the same message."""
    if whatsapp_message_id:
        existing = find_by_whatsapp_message_id(whatsapp_message_id, session)
        if existing is not None:
            return existing

    document = IncomingDocument(
        source=source,
        filename=filename,
        sender=sender,
        whatsapp_message_id=whatsapp_message_id,
        status=IncomingDocumentStatus.RECEIVED,
    )
    session.add(document)
    session.flush()  # assign document.id
    return document


def set_document_type(
    document: IncomingDocument, document_type: IncomingDocumentType, session: Session
) -> None:
    document.document_type = document_type
    session.flush()


def mark_processed(
    document: IncomingDocument,
    session: Session,
    *,
    has_errors: bool = False,
    inventory_import_id: int | None = None,
    customer_order_id: int | None = None,
    delivery_import_id: int | None = None,
) -> IncomingDocument:
    document.status = (
        IncomingDocumentStatus.PROCESSED_WITH_ERRORS
        if has_errors
        else IncomingDocumentStatus.PROCESSED
    )
    document.inventory_import_id = inventory_import_id
    document.customer_order_id = customer_order_id
    document.delivery_import_id = delivery_import_id
    document.processed_at = _utcnow()
    session.flush()
    return document


def mark_failed(document: IncomingDocument, error_message: str, session: Session) -> IncomingDocument:
    document.status = IncomingDocumentStatus.FAILED
    document.error_message = error_message
    document.processed_at = _utcnow()
    session.flush()
    return document


def mark_download_failed(
    document: IncomingDocument, error_message: str, session: Session
) -> IncomingDocument:
    """Distinct from `mark_failed`: this document never even reached the
    Document Processing Engine -- the WhatsApp media download itself failed.
    Kept as its own status (rather than folded into FAILED) so the
    Integration Status page can report "Media Download Status" without
    string-matching an error message."""
    document.status = IncomingDocumentStatus.DOWNLOAD_FAILED
    document.error_message = error_message
    document.processed_at = _utcnow()
    session.flush()
    return document


def mark_skipped_duplicate(
    document: IncomingDocument, message: str, session: Session
) -> IncomingDocument:
    document.status = IncomingDocumentStatus.SKIPPED_DUPLICATE
    document.error_message = message
    document.processed_at = _utcnow()
    session.flush()
    return document


def mark_needs_review(document: IncomingDocument, session: Session) -> IncomingDocument:
    document.status = IncomingDocumentStatus.NEEDS_REVIEW
    document.error_message = (
        "Could not determine document type automatically -- needs manual review."
    )
    document.processed_at = _utcnow()
    session.flush()
    return document


def mark_unsupported(document: IncomingDocument, message: str, session: Session) -> IncomingDocument:
    document.status = IncomingDocumentStatus.UNSUPPORTED
    document.error_message = message
    document.processed_at = _utcnow()
    session.flush()
    return document


def list_incoming_documents(
    session: Session,
    *,
    source: DocumentSource | None = None,
    document_type: IncomingDocumentType | None = None,
    status: IncomingDocumentStatus | None = None,
    limit: int = 50,
) -> list[IncomingDocument]:
    statement = select(IncomingDocument).order_by(IncomingDocument.received_at.desc())
    if source is not None:
        statement = statement.where(IncomingDocument.source == source)
    if document_type is not None:
        statement = statement.where(IncomingDocument.document_type == document_type)
    if status is not None:
        statement = statement.where(IncomingDocument.status == status)
    statement = statement.limit(limit)

    return list(session.execute(statement).scalars())
