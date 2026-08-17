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

from sqlalchemy import CheckConstraint, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base

STATUS_ROW_ID = 1


class WhatsAppRegisteredNumber(Base):
    """The permanent identity layer for direct vendor/customer uploads:
    WhatsApp number -> Vendor (or Customer). A file from a registered number
    needs NO command, NO caption, and NO filename convention -- the number
    alone identifies the party (see `registry.py` / the document worker).

    Rules enforced here:
    - one identity per number (`whatsapp_number` unique; the CheckConstraint
      guarantees the row points at EXACTLY one of vendor/customer -- the
      business rule that a number never sends both kinds of files);
    - many numbers per party are fine (owner + staff numbers both map to the
      same vendor).

    Numbers are stored NORMALIZED (digits only, with country code -- e.g.
    "919212552626"), matching the wa_id format Meta delivers in webhooks;
    `registry.normalize_number` is the single place that shapes them."""

    __tablename__ = "whatsapp_registered_numbers"
    __table_args__ = (
        CheckConstraint(
            "(vendor_id IS NOT NULL AND customer_id IS NULL) OR "
            "(vendor_id IS NULL AND customer_id IS NOT NULL)",
            name="ck_whatsapp_registered_number_one_party",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(unique=True, index=True)
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="CASCADE"), default=None, index=True
    )
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), default=None, index=True
    )
    # Free-text label for the admin ("owner", "staff - Ramesh") -- never used
    # for identity.
    note: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


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


class WhatsAppVendorMemory(Base):
    """The vendor name a WhatsApp number most recently supplied (caption or
    follow-up text), remembered so that MORE files from the same sender
    within the grouping window (`WHATSAPP_GROUPING_WINDOW_MINUTES`) are
    grouped under the SAME vendor automatically -- no re-asking per file.
    One row per number; `updated_at` is the freshness clock."""

    __tablename__ = "whatsapp_vendor_memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(unique=True, index=True)
    vendor_name: Mapped[str] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )


class PurchaseTeamMember(Base):
    """An internal purchase-team member (the Founder's clarification: people
    like Alam/Rajkumar who send orders on WhatsApp and must receive every
    generated PO as a CC). Managed by the Founder over WhatsApp: text
    `register team`, then send an Excel of Name + WhatsApp number -- the
    list REPLACES the previous one, exactly like the vendor contact flow.
    Numbers stored normalized (see `registry.normalize_number`)."""

    __tablename__ = "purchase_team_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    whatsapp_number: Mapped[str] = mapped_column(unique=True, index=True)
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
