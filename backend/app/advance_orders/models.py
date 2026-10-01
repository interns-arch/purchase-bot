"""Tables for advance orders. Shares `core.models.Base`, so `init_db()` creates
them at startup (they are new tables -- nothing existing is altered).

- `VendorBrand`: which vendor supplies which brand, and in what order to ask.
  Brand "*" is the fallback list for a part whose brand has no vendors.
- `AdvanceOrder` / `AdvanceOrderLine`: one request from the sales bot.
- `AdvanceVendorQuery`: one question to one vendor about one brand's lines of
  one order -- the history of who was asked, when, and what they said.

Timestamps are naive IST, the application-wide convention (core/time_utils)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, Index, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.models import Base

# AdvanceOrder.status
ASKING = "asking"  # vendors are being asked
QUOTED = "quoted"  # every line has an answer, at least one vendor has it
NO_VENDOR = "no_vendor"  # every line has an answer, nobody has any of it
CONFIRMED = "confirmed"  # the customer said yes; the vendors were told
CANCELLED = "cancelled"

# AdvanceOrder.kind
KIND_ADVANCE = "advance"  # case 3: not in stock anywhere -- availability, then the customer confirms
KIND_DEALER_STOCK = "dealer_stock"  # case 2: a vendor has it -- ORDER it from him now

# AdvanceOrderLine.status
LINE_ASKING = "asking"
LINE_AVAILABLE = "available"
LINE_UNAVAILABLE = "unavailable"
LINE_ORDERED = "ordered"
LINE_CANCELLED = "cancelled"

# AdvanceVendorQuery.status
Q_QUEUED = "queued"  # waiting for vendor hours
Q_SENT = "sent"
Q_REPLIED = "replied"
Q_TIMEOUT = "timeout"
Q_FAILED = "failed"  # could not be delivered
Q_CLOSED = "closed"  # the order was cancelled while it was open

# VendorBrand.discount_type -- how this vendor prices this brand.
#   PERCENT: a standing discount off MRP, already known -- never asked.
#   RATE:    the vendor quotes a net rate per part; it MUST be asked.
#   OTHER:   an arrangement no formula covers ("rate + scheme", "1000
#            discount"). Treated as RATE for asking, and never ranked on
#            price -- a human decides. Nothing is inferred.
DISC_PERCENT = "percent"
DISC_RATE = "rate"
DISC_OTHER = "other"


class VendorBrand(Base):
    __tablename__ = "vendor_brands"
    __table_args__ = (UniqueConstraint("brand", "vendor_id", name="ux_vendor_brands_brand_vendor"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored UPPER-CASE ("MARUTI", "HYUNDAI", "*").
    brand: Mapped[str] = mapped_column(index=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    # 1 is asked first.
    priority: Mapped[int] = mapped_column(default=100)
    active: Mapped[bool] = mapped_column(default=True)

    # --- standing commercial terms (VENDOR BRAND MAPPING.xlsx) -------------
    # The discount is a STANDING term, agreed once with the vendor -- not
    # something the bot asks for on every enquiry. 61 of the Founder's 111
    # rows carry a fixed percentage; the rest quote a net rate instead.
    # Knowing it up front is what keeps the WhatsApp question down to "do you
    # have it, and in how many days", which is the whole reason vendor
    # replies can be parsed safely.
    discount_type: Mapped[str] = mapped_column(default=DISC_PERCENT)
    # Percent off MRP. Only meaningful when discount_type == PERCENT.
    discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), default=None)
    # The sheet's own wording, kept verbatim for anything a number cannot
    # carry ("rate + scheme", "1000 discount"). Shown to the desk, never parsed.
    discount_note: Mapped[str | None] = mapped_column(default=None)
    transport: Mapped[str | None] = mapped_column(default=None)
    payment_terms: Mapped[str | None] = mapped_column(default=None)
    # The Founder's sheet says only 7 of 111 vendor+brand rows will ever send
    # a stock file. The other 104 are exactly why this feature exists, and
    # this flag is what tells the two worlds apart.
    can_share_stock: Mapped[bool | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class AdvanceOrder(Base):
    __tablename__ = "advance_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # The sales bot's own id ("ADV-MU3WLV1K"); a repeat POST with the same one
    # returns the existing order instead of asking the vendors twice.
    external_ref: Mapped[str | None] = mapped_column(default=None, index=True)
    source: Mapped[str] = mapped_column(default="autoflow")
    customer_portal_id: Mapped[str | None] = mapped_column(default=None)
    customer_name: Mapped[str | None] = mapped_column(default=None)
    customer_phone: Mapped[str | None] = mapped_column(default=None)
    requested_by: Mapped[str | None] = mapped_column(default=None)
    needed_by: Mapped[date | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(default=ASKING, index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)
    # "advance" (case 3) or "dealer_stock" (case 2) -- see KIND_*.
    kind: Mapped[str] = mapped_column(default=KIND_ADVANCE)
    # Case 2's overall window (Founder: 12 hours). Lines still open at this
    # moment are reported as not found, to a person.
    deadline_at: Mapped[datetime | None] = mapped_column(default=None)

    lines: Mapped[list[AdvanceOrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="AdvanceOrderLine.id"
    )


class AdvanceOrderLine(Base):
    __tablename__ = "advance_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    advance_order_id: Mapped[int] = mapped_column(ForeignKey("advance_orders.id", ondelete="CASCADE"), index=True)
    part_number: Mapped[str] = mapped_column()
    part_name: Mapped[str | None] = mapped_column(default=None)
    # Upper-case, or "*" when nobody knows it.
    brand: Mapped[str] = mapped_column(default="*")
    qty: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(default=LINE_ASKING)
    available_qty: Mapped[int | None] = mapped_column(default=None)
    eta_date: Mapped[date | None] = mapped_column(default=None)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"), default=None)
    note: Mapped[str | None] = mapped_column(default=None)
    # Case 2: the Dealer Portal dealer whose stock showed the part, when the
    # sales bot knows it -- that vendor is asked first.
    dealer_id: Mapped[int | None] = mapped_column(default=None)
    # Case 2: the vendor's stock file (InventoryImport id) this line was
    # ordered against. Pieces ordered count against THAT file only, so his
    # next stock file starts the count afresh -- by identity, not by clock.
    stock_import_id: Mapped[int | None] = mapped_column(default=None)

    # --- the winning quote, copied here when the line is decided -----------
    # `advance_vendor_quotes` keeps every vendor's answer; these columns hold
    # the one that won, so the sales bot reads a line without joining.
    tat_days: Mapped[int | None] = mapped_column(default=None)
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), default=None)
    # Per unit, net of discount. NULL whenever no honest figure exists -- an
    # unpriced line is never shown as zero (the same rule Price Leakage
    # follows in the Command Centre).
    net_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    winning_quote_id: Mapped[int | None] = mapped_column(default=None)

    order: Mapped[AdvanceOrder] = relationship(back_populates="lines")


class AdvanceVendorQuery(Base):
    __tablename__ = "advance_vendor_queries"
    __table_args__ = (Index("ix_advance_vendor_queries_status", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    advance_order_id: Mapped[int] = mapped_column(ForeignKey("advance_orders.id", ondelete="CASCADE"), index=True)
    brand: Mapped[str] = mapped_column()
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"))
    line_ids: Mapped[list] = mapped_column(JSON, default=list)
    # The vendor's WhatsApp numbers this was sent to; a reply from any of them counts.
    numbers: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(default=Q_QUEUED)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(default=None)
    deadline_at: Mapped[datetime | None] = mapped_column(default=None)
    replied_at: Mapped[datetime | None] = mapped_column(default=None)
    reply_text: Mapped[str | None] = mapped_column(default=None)


class AdvanceVendorQuote(Base):
    """One vendor's answer about ONE line.

    The point of a separate table: several vendors are asked about the same
    part, and every answer has to survive so the ranking can compare them.
    Writing the answer onto the line directly -- which is what the
    first-vendor-wins flow did -- means the second vendor's better discount
    overwrites the first one's, or is thrown away. Here nothing is lost, and
    `ranking.best_quote()` decides which one the customer is told about.

    `net_price` is only ever set when it can be computed honestly: a quoted
    rate, or an MRP with a standing percentage. A vendor who says "haan, 3
    din" and nothing else leaves it NULL -- available, with no price, which
    the desk can see and act on."""

    __tablename__ = "advance_vendor_quotes"
    __table_args__ = (
        UniqueConstraint("advance_order_line_id", "vendor_id", name="ux_advance_quote_line_vendor"),
        Index("ix_advance_vendor_quotes_order", "advance_order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    advance_order_id: Mapped[int] = mapped_column(
        ForeignKey("advance_orders.id", ondelete="CASCADE"), index=True
    )
    advance_order_line_id: Mapped[int] = mapped_column(
        ForeignKey("advance_order_lines.id", ondelete="CASCADE"), index=True
    )
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    # The question this answered; NULL for an answer an admin typed in.
    query_id: Mapped[int | None] = mapped_column(default=None)

    available: Mapped[bool] = mapped_column(default=False)
    available_qty: Mapped[int | None] = mapped_column(default=None)
    tat_days: Mapped[int | None] = mapped_column(default=None)
    eta_date: Mapped[date | None] = mapped_column(default=None)

    mrp: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    # What the vendor actually said his rate is (RATE vendors only).
    quoted_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), default=None)
    net_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    # "standing" (from vendor_brands), "quoted" (the vendor said a rate),
    # or "none" -- so the desk always knows where a number came from.
    price_source: Mapped[str] = mapped_column(default="none")

    # Kept verbatim. When a figure is ever questioned, this is the evidence.
    raw_reply: Mapped[str | None] = mapped_column(default=None)
    source: Mapped[str] = mapped_column(default="whatsapp")  # or "admin"
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AdvanceVendorContact(Base):
    """A WhatsApp number to ASK a vendor on -- deliberately NOT the registry.

    WHY NOT `whatsapp_registered_numbers`
    ------------------------------------
    Registering a number there changes how the live system treats it: every
    registered vendor number receives the 09:30 "please share your stock"
    template (`daily_stock.send_morning_requests` has no filter), counts as
    pending in the 11:00 summary until it uploads, and has any Excel it sends
    auto-imported as stock. The Founder's mapping says 104 of 111 vendor rows
    will NEVER share stock -- registering them would message those vendors
    every morning and leave them permanently "pending".

    So enquiry contacts live here. Nothing outside advance orders reads this
    table. A reply still reaches us, because `handle_vendor_text` matches a
    reply against the numbers the question was SENT to, and it runs before
    the registry is consulted (`document_worker._handle_incoming_whatsapp_text`).

    Numbers are stored normalised, the same shape as the registry."""

    __tablename__ = "advance_vendor_contacts"
    __table_args__ = (
        UniqueConstraint("vendor_id", "whatsapp_number", name="ux_advance_contact_vendor_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), index=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    # Where it came from ("VENDOR BRAND MAPPING.xlsx", "desk").
    source: Mapped[str | None] = mapped_column(default=None)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PartBrandHint(Base):
    """Which brand a part number belongs to, for routing an enquiry.

    Kept apart from `parts.brand` on purpose. The `parts` table is the
    canonical master that inventory, allocation and Part Intelligence all
    read; filling 9,000 rows of it from a spreadsheet is a change to shared
    data. This table is read by exactly one function -- `service._brand_for_line`
    -- and only when the sales bot sent no brand and `parts.brand` is empty.

    `part_number` is stored NORMALISED (letters and digits only, upper case),
    the same form as `parts.canonical_part_number`, so "16510-M68K10" and
    "16510M68K10" find the same hint."""

    __tablename__ = "part_brand_hints"

    id: Mapped[int] = mapped_column(primary_key=True)
    part_number: Mapped[str] = mapped_column(unique=True, index=True)
    brand: Mapped[str] = mapped_column()
    source: Mapped[str | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
