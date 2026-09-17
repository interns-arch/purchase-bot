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

from sqlalchemy import JSON, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.models import Base

# AdvanceOrder.status
ASKING = "asking"  # vendors are being asked
QUOTED = "quoted"  # every line has an answer, at least one vendor has it
NO_VENDOR = "no_vendor"  # every line has an answer, nobody has any of it
CONFIRMED = "confirmed"  # the customer said yes; the vendors were told
CANCELLED = "cancelled"

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
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


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
