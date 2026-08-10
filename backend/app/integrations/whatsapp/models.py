"""Persisted state for the Integration Status page -- a single row tracking
the last webhook verification and the last live connection test, so the
admin can see current integration health without re-triggering anything.
Shares `core.models.Base`/the same database, same reasoning as
`backend/app/documents/models.py`: this is a web-app/integration concept,
not core business logic.

Also holds `WhatsAppPendingCommand`: the per-number routing command
(`vendor` / `customer` / future ...) a WhatsApp user must send BEFORE
uploading a file, so the system knows which import workflow to run for the
next file from that number."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base

STATUS_ROW_ID = 1


class WhatsAppPendingCommand(Base):
    """The latest valid routing command a WhatsApp number sent, remembered
    until that number's next file is processed (then cleared). Exactly one
    row per number (`whatsapp_number` unique), so multiple users route
    independently and concurrently. `command` stores the canonical command
    key from `backend.app.integrations.whatsapp.commands` (e.g. "vendor",
    "customer"), never raw user text."""

    __tablename__ = "whatsapp_pending_commands"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(unique=True, index=True)
    command: Mapped[str] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class WhatsAppPendingVendorFile(Base):
    """A staged Vendor Inventory file waiting for its VENDOR NAME.

    Vendor identity comes from the name the sender supplies (file caption, or
    a follow-up text) -- NEVER from the filename. When a vendor file arrives
    without a caption, it is staged on disk and recorded here; the sender's
    next non-command text message is taken as the vendor name and every
    pending file for that number is then imported for that vendor. Persisted
    in the DB (same reasoning as `WhatsAppPendingCommand`) so a restart never
    loses the association. `original_filename` is audit metadata only."""

    __tablename__ = "whatsapp_pending_vendor_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    staged_path: Mapped[str] = mapped_column()
    original_filename: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WhatsAppIntegrationStatus(Base):
    __tablename__ = "whatsapp_integration_status"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_webhook_verified_at: Mapped[datetime | None] = mapped_column(default=None)
    last_connection_tested_at: Mapped[datetime | None] = mapped_column(default=None)
    last_connection_success: Mapped[bool | None] = mapped_column(default=None)
    last_connection_message: Mapped[str | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
