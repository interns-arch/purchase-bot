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

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, func
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


class WhatsAppPendingCustomerFile(Base):
    """A staged Customer Order file waiting for its CUSTOMER NAME.

    The exact counterpart of `WhatsAppPendingVendorFile` (Founder, 1 Sep
    2026: an unregistered number must be able to send a customer order "like
    vendor file is treating now"). An UNREGISTERED sender's Customer Order
    file names its customer through the file caption or a follow-up text; if
    neither is present the file is staged on disk and recorded here, and the
    sender's next non-command text is taken as the customer name.

    A REGISTERED customer number never reaches this table -- the number is
    the identity. `original_filename` is audit metadata only."""

    __tablename__ = "whatsapp_pending_customer_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    staged_path: Mapped[str] = mapped_column()
    original_filename: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SalesTeamMember(Base):
    """An internal SALES-team member (Founder, 3 Sep 2026).

    These are the people who ask "is this part in our inventory?" over
    WhatsApp and need an instant answer. Deliberately a separate party type
    from `WhatsAppRegisteredNumber`: a sales number must NOT be registered as
    a customer, or their question would be imported as a customer ORDER
    instead of answered as a QUERY.

    Managed by the Founder over WhatsApp: text `register sales`, then send
    the list (Excel, or typed straight into the chat). Numbers stored
    normalized (see `registry.normalize_number`)."""

    __tablename__ = "sales_team_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(nullable=False)
    whatsapp_number: Mapped[str] = mapped_column(unique=True, index=True)
    # The customer this member last placed an order for, so a bare "confirm"
    # can OFFER it instead of making them retype. Only ever offered, never
    # applied silently: one sales person serves MANY customers, and quietly
    # reusing yesterday's name would file the order against the wrong one.
    last_customer_name: Mapped[str | None] = mapped_column(default=None)
    last_customer_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SalesQuote(Base):
    """A stock answer given to a sales member, held so they can CONFIRM it
    into a real order with one word instead of retyping the parts.

    Founder, 3 Sep 2026: the sales team asks what is available, and once they
    say yes the allocation must reach the PURCHASE team -- never back to the
    sales team with vendor names.

    Why the quote is stored rather than the order created immediately: a
    stock check is a QUESTION. Creating an order for every question would
    reserve stock nobody asked for. And why it is RE-CHECKED at confirm time
    rather than trusted: between the question and the yes, another order may
    have taken the same stock, so the quantities quoted here are a snapshot
    for display only -- never the authority.

    `parts` is [{"part_no": str, "quantity": str|null, "available": str}].
    """

    __tablename__ = "sales_quotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True, nullable=False)
    member_name: Mapped[str | None] = mapped_column(default=None)
    reference: Mapped[str] = mapped_column(index=True, nullable=False)  # e.g. "A3"
    parts: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(default="PENDING")  # PENDING/CONFIRMED/EXPIRED
    customer_name: Mapped[str | None] = mapped_column(default=None)
    customer_order_id: Mapped[int | None] = mapped_column(default=None)
    nudged_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        Index("ix_sales_quotes_number_status", "whatsapp_number", "status"),
    )
