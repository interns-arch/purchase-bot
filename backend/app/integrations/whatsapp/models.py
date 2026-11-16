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
from decimal import Decimal

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Numeric, UniqueConstraint, func
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


class WhatsAppPendingPdfChoice(Base):
    """A registered vendor's PDF that could not be told apart as a stock list
    or a bill, held while the bot asks him "stock list hai ya bill?".

    Its own table, not `whatsapp_pending_vendor_files`: that one holds files
    waiting for a VENDOR NAME, and a reply meant for one question must never
    be read as the answer to the other. Persisted so a restart keeps it."""

    __tablename__ = "whatsapp_pending_pdf_choices"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"))
    staged_path: Mapped[str] = mapped_column()
    original_filename: Mapped[str] = mapped_column()
    media_id: Mapped[str | None] = mapped_column(default=None)
    message_id: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WhatsAppPendingPhotoStock(Base):
    """Stock lines read off a vendor's PHOTO (or scanned PDF), waiting for him
    to confirm "haan" before they are imported.

    Only the LINES are kept, never treated as stock until he agrees: a
    misread digit is caught by the one person who knows his own stock.
    One row per number -- a newer photo replaces an unanswered older one."""

    __tablename__ = "whatsapp_pending_photo_stock"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"))
    # [{"part": "...", "qty": "5", "description": "..."}]
    lines: Mapped[list] = mapped_column(JSON, default=list)
    unreadable: Mapped[list] = mapped_column(JSON, default=list)
    source_filename: Mapped[str | None] = mapped_column(default=None)
    model: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WhatsAppChatMessage(Base):
    """EVERY WhatsApp message on the line, in and out -- what the Chats page
    shows. `direction` "in" (someone wrote) / "out" (the bot sent);
    `kind` text / document / image / template; `status` received / sent /
    delivered / read / failed, kept current from WhatsApp's delivery reports
    (matched by `wamid`)."""

    __tablename__ = "whatsapp_chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    direction: Mapped[str] = mapped_column()
    kind: Mapped[str] = mapped_column(default="text")
    text: Mapped[str | None] = mapped_column(default=None)
    filename: Mapped[str | None] = mapped_column(default=None)
    media_id: Mapped[str | None] = mapped_column(default=None)
    wamid: Mapped[str | None] = mapped_column(default=None, index=True)
    status: Mapped[str] = mapped_column(default="received")
    error: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
    # Who typed an outgoing message by hand during a human takeover
    # (integrations/whatsapp/takeover.py); None = the bot.
    sent_by: Mapped[str | None] = mapped_column(default=None)


class WhatsAppTakeover(Base):
    """HUMAN TAKEOVER of one chat (founder, 28 Oct, like the sales bot's): a
    person replies by hand from the Chats page and the bot says nothing
    there until it is handed back. One row per number, kept for good -- a
    takeover lasts until someone hands it back, restarts included.

    `pending`: what they sent since our last successful send (answered at
    hand-back). `held`: what the bot wanted to send meanwhile, for the admin
    to send or drop. `audit`: every switch, send and drop, with who."""

    __tablename__ = "whatsapp_takeovers"

    whatsapp_number: Mapped[str] = mapped_column(primary_key=True)
    active: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(default=0)
    by: Mapped[str | None] = mapped_column(default=None)
    since: Mapped[datetime | None] = mapped_column(default=None)
    changed_at: Mapped[datetime | None] = mapped_column(default=None)
    handed_back_at: Mapped[datetime | None] = mapped_column(default=None)
    pending: Mapped[list | None] = mapped_column(JSON, default=None)
    held: Mapped[list | None] = mapped_column(JSON, default=None)
    handled: Mapped[dict | None] = mapped_column(JSON, default=None)
    resume: Mapped[dict | None] = mapped_column(JSON, default=None)
    audit: Mapped[list | None] = mapped_column(JSON, default=None)


class AiConversationMessage(Base):
    """What a staff member and the purchase bot's AI assistant said to each
    other -- the assistant's memory, like the sales bot's (AutoFlow keeps the
    last 20 messages per chat in Postgres). "hi" from staff starts afresh.
    `role`: "in" (staff wrote), "out" (bot replied), "pending" (an action
    waiting for the staff member's haan, e.g. sending reminders)."""

    __tablename__ = "ai_conversation_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    role: Mapped[str] = mapped_column()
    text: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WhapiVendorGroup(Base):
    """A vendor's WhatsApp group the bot works in (Whapi, 29 Oct 2026): the
    existing groups linked by hand and the ones it creates for each new
    vendor. Read together with WHAPI_VENDOR_GROUPS; one vendor may have
    several groups."""

    __tablename__ = "whapi_vendor_groups"

    group_id: Mapped[str] = mapped_column(primary_key=True)
    vendor_code: Mapped[str] = mapped_column(index=True)
    name: Mapped[str | None] = mapped_column(default=None)
    created_by_bot: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VendorMrpList(Base):
    """One MRP list a vendor sent (see `mrp_list.py`); a row for this month means
    that vendor is not asked again."""

    __tablename__ = "vendor_mrp_lists"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    month: Mapped[str] = mapped_column(index=True)  # "2026-11"
    file_name: Mapped[str | None] = mapped_column(default=None)
    parts: Mapped[int] = mapped_column(default=0)
    bad_rows: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VendorMrpItem(Base):
    """A vendor's latest MRP for one part, from their monthly list."""

    __tablename__ = "vendor_mrp_items"
    __table_args__ = (UniqueConstraint("vendor_id", "normalized_part_number", name="ux_vendor_mrp_items_part"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), index=True)
    normalized_part_number: Mapped[str] = mapped_column(index=True)
    part_no: Mapped[str | None] = mapped_column(default=None)
    description: Mapped[str | None] = mapped_column(default=None)
    mrp: Mapped[Decimal] = mapped_column(Numeric(18, 4))
    list_month: Mapped[str] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class WhapiActivity(Base):
    """What the bot did in the WhatsApp groups, one row per action (2 Nov
    2026) -- the live feed on the console's Groups page: a file or question
    taken from a group, a reply or stock request posted, a group created."""

    __tablename__ = "whapi_activity"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[str] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column()
    who: Mapped[str | None] = mapped_column(default=None)
    text: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
