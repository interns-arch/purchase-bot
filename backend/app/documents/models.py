"""Incoming Documents table -- one row per file the system ever received,
manual upload or WhatsApp, before (and regardless of whether) it was
successfully processed. Deliberately separate from `core/models.py` (the
business schema) even though both share the same `Base`/database, same
reasoning as `backend/app/auth/models.py`: this is a web-app/integration
concept the CLI scripts never touch, not core business logic."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base


class DocumentSource(str, enum.Enum):
    MANUAL = "MANUAL"
    WHATSAPP = "WHATSAPP"


class IncomingDocumentType(str, enum.Enum):
    VENDOR_INVENTORY = "VENDOR_INVENTORY"
    CUSTOMER_ORDER = "CUSTOMER_ORDER"
    DELIVERY = "DELIVERY"
    VENDOR_INVOICE = "VENDOR_INVOICE"
    UNKNOWN = "UNKNOWN"


class IncomingDocumentStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PROCESSED = "PROCESSED"
    PROCESSED_WITH_ERRORS = "PROCESSED_WITH_ERRORS"
    FAILED = "FAILED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    UNSUPPORTED = "UNSUPPORTED"


class IncomingDocument(Base):
    __tablename__ = "incoming_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[DocumentSource] = mapped_column(
        Enum(DocumentSource, name="document_source"), nullable=False
    )
    document_type: Mapped[IncomingDocumentType] = mapped_column(
        Enum(IncomingDocumentType, name="incoming_document_type"),
        default=IncomingDocumentType.UNKNOWN,
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(nullable=False)
    sender: Mapped[str | None] = mapped_column(default=None)
    whatsapp_message_id: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[IncomingDocumentStatus] = mapped_column(
        Enum(IncomingDocumentStatus, name="incoming_document_status"),
        default=IncomingDocumentStatus.RECEIVED,
        nullable=False,
    )
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)

    inventory_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_imports.id", ondelete="SET NULL"), default=None
    )
    customer_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("customer_orders.id", ondelete="SET NULL"), default=None
    )
    delivery_import_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_imports.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (
        Index(
            "ux_incoming_documents_whatsapp_message_id",
            whatsapp_message_id,
            unique=True,
            sqlite_where=text("whatsapp_message_id IS NOT NULL"),
            postgresql_where=text("whatsapp_message_id IS NOT NULL"),
        ),
        Index("ix_incoming_documents_received_at", "received_at"),
    )
