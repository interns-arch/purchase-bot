"""Tables for Portal procurement. New tables only -- `init_db()` creates them
at startup; nothing existing is altered.

    portal_demand_lines   one Portal source demand line (source_order_line_id)
    portal_allocations    one accepted quote -> one POST /active/allocate
    portal_po_masters     one PO-MST the Portal returned -> knockoff, approval,
                          Odoo, PDF, WhatsApp
    portal_transitions    every state change, for the audit trail

State changes go through `service._move()`, a compare-and-set UPDATE, so two
workers can never take the same row through the same step. Timestamps are
naive IST, the application-wide convention."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, Numeric
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base
from core.time_utils import now_ist_naive

# ---- demand line states (brief: required bot state machine, 1-5)
DISCOVERED = "discovered"
ENQUIRY_SCHEDULED = "enquiry_scheduled"
AWAITING_VENDOR_REPLIES = "awaiting_vendor_replies"
QUOTES_NORMALIZED = "quotes_normalized"
WINNER_SELECTED = "winner_selected"
MARGIN_HOLD = "margin_hold"  # no vendor meets the minimum margin -- the team decides
NO_VENDOR = "no_vendor"  # every vendor said no / nobody answered -- told to a person
CLOSED = "closed"  # no longer pending on the Portal, or its quantity went to 0
COMPLETED = "completed"  # fully allocated and every PO-MST completed

# ---- allocation states (6-8)
VENDOR_ONBOARDING_REQUIRED = "vendor_onboarding_required"
VENDOR_MAPPED = "vendor_mapped"
ALLOCATION_SUBMITTING = "allocation_submitting"
ALLOCATION_UNKNOWN = "allocation_unknown"  # POST outcome lost -- reconcile by GET
PO_MST_CREATED = "po_mst_created"
ALLOCATION_FAILED = "allocation_failed"  # Portal refused (4xx) -- needs a person
ALLOCATION_CANCELLED = "allocation_cancelled"  # demand shrank / closed before allocation

# ---- PO-MST states (9-16)
KNOCKOFF_PREVIEWED = "knockoff_previewed"
SAFE_KNOCKOFF_COMMITTED = "safe_knockoff_committed"
APPROVAL_PENDING = "approval_pending"
APPROVAL_APPROVED = "approval_approved"
APPROVAL_REJECTED = "approval_rejected"
ODOO_POSTING = "odoo_posting"
ODOO_POSTED = "odoo_posted"
PDF_DOWNLOADED = "pdf_downloaded"
PURCHASE_USER_NOTIFIED = "purchase_user_notified"
NEEDS_REVIEW = "needs_review"  # a 409/422 or an answer the bot cannot read -- a person decides
DEAD_LETTER = "dead_letter"  # failed max_step_attempts times


class PortalDemandLine(Base):
    __tablename__ = "portal_demand_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_order_line_id: Mapped[int] = mapped_column(unique=True, index=True)
    source_po_group_id: Mapped[str | None] = mapped_column(default=None, index=True)
    part_no: Mapped[str] = mapped_column()
    brand: Mapped[str | None] = mapped_column(default=None)
    planned_quantity: Mapped[int] = mapped_column(default=0)
    order_date: Mapped[date | None] = mapped_column(default=None)
    # Buyer branch / dealer context exactly as the Portal sent it.
    buyer: Mapped[dict | None] = mapped_column(JSON, default=None)
    procurement_stage: Mapped[str | None] = mapped_column(default=None)
    row_hash: Mapped[str | None] = mapped_column(default=None)
    state: Mapped[str] = mapped_column(default=DISCOVERED, index=True)
    advance_order_id: Mapped[int | None] = mapped_column(default=None, index=True)
    allocated_quantity: Mapped[int] = mapped_column(default=0)
    note: Mapped[str | None] = mapped_column(default=None)
    first_seen_at: Mapped[datetime] = mapped_column(default=now_ist_naive)
    last_seen_at: Mapped[datetime | None] = mapped_column(default=None)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist_naive)
    # The customer's side, from the Portal row (14 Nov 2026), for the minimum
    # margin: MRP, the customer's discount % and the price he pays (incl. GST).
    portal_mrp: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    customer_discount_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), default=None)
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)


class PortalAllocation(Base):
    __tablename__ = "portal_allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    demand_line_id: Mapped[int] = mapped_column(ForeignKey("portal_demand_lines.id", ondelete="CASCADE"), index=True)
    advance_order_line_id: Mapped[int | None] = mapped_column(default=None, unique=True)
    quote_id: Mapped[int | None] = mapped_column(default=None)
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id", ondelete="SET NULL"), default=None)
    supplier_dealer_id: Mapped[int | None] = mapped_column(default=None)
    quantity: Mapped[int] = mapped_column()
    quoted_mrp_incl_gst: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    quoted_rate_ex_gst: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), default=None)
    quoted_discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), default=None)
    quoted_tat_days: Mapped[int | None] = mapped_column(default=None)
    # Stable ProcureHub ids: the quote reference goes to the Portal; the
    # idempotency key guards our own POST.
    quote_reference: Mapped[str] = mapped_column(unique=True)
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    ranking_reason: Mapped[str | None] = mapped_column(default=None)
    state: Mapped[str] = mapped_column(default=WINNER_SELECTED, index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    batch_id: Mapped[str | None] = mapped_column(default=None)
    masters: Mapped[list | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    error_status: Mapped[int | None] = mapped_column(default=None)
    request_id: Mapped[str | None] = mapped_column(default=None)
    next_try_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=now_ist_naive)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist_naive)


class PortalPoMaster(Base):
    __tablename__ = "portal_po_masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    po_mst: Mapped[str] = mapped_column(unique=True, index=True)
    allocation_ids: Mapped[list] = mapped_column(JSON, default=list)
    vendor_dealer_id: Mapped[int | None] = mapped_column(default=None)
    vendor_name: Mapped[str | None] = mapped_column(default=None)
    brand: Mapped[str | None] = mapped_column(default=None)
    quantity: Mapped[int | None] = mapped_column(default=None)
    arrival_date: Mapped[str | None] = mapped_column(default=None)
    state: Mapped[str] = mapped_column(default=PO_MST_CREATED, index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    # Knockoff preview, as quantities and ids (never UI text).
    preview: Mapped[dict | None] = mapped_column(JSON, default=None)
    safe_qty: Mapped[int | None] = mapped_column(default=None)
    blocked_qty: Mapped[int | None] = mapped_column(default=None)
    future_qty: Mapped[int | None] = mapped_column(default=None)
    safe_line_ids: Mapped[list | None] = mapped_column(JSON, default=None)
    approval_status: Mapped[str | None] = mapped_column(default=None)
    approval_reason: Mapped[str | None] = mapped_column(default=None)
    odoo_ref: Mapped[str | None] = mapped_column(default=None)
    odoo_name: Mapped[str | None] = mapped_column(default=None)
    pdf_sha256: Mapped[str | None] = mapped_column(default=None)
    pdf_size: Mapped[int | None] = mapped_column(default=None)
    # Where the PDF went: {number: wamid}; numbers still waiting for a 24 h
    # window (they get it the next time they write to the bot).
    notified: Mapped[dict | None] = mapped_column(JSON, default=None)
    notify_pending: Mapped[list | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    error_status: Mapped[int | None] = mapped_column(default=None)
    request_id: Mapped[str | None] = mapped_column(default=None)
    next_try_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=now_ist_naive)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist_naive)


class PortalTransition(Base):
    __tablename__ = "portal_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity: Mapped[str] = mapped_column()  # "demand" | "allocation" | "po_mst"
    entity_id: Mapped[int] = mapped_column(index=True)
    source_order_line_id: Mapped[int | None] = mapped_column(default=None, index=True)
    quote_reference: Mapped[str | None] = mapped_column(default=None)
    po_mst: Mapped[str | None] = mapped_column(default=None)
    from_state: Mapped[str | None] = mapped_column(default=None)
    to_state: Mapped[str] = mapped_column()
    actor: Mapped[str] = mapped_column(default="bot")
    http_status: Mapped[int | None] = mapped_column(default=None)
    request_id: Mapped[str | None] = mapped_column(default=None)
    error: Mapped[str | None] = mapped_column(default=None)
    at: Mapped[datetime] = mapped_column(default=now_ist_naive)


# ---- vendor -> Portal dealer links for PROCUREMENT ONLY (12 Nov 2026)
LINK_LINKED = "linked"
LINK_ONBOARDING = "onboarding_required"  # a person said N: not on the Portal yet


class PortalVendorLink(Base):
    """Which Portal dealer a bot vendor IS, for creating PO-MSTs.

    Deliberately NOT `dealer_portal_vendor_map`: that table decides which
    Portal account a vendor's STOCK is uploaded to, and an import here must
    never change stock uploads. An admin-confirmed row there always wins over
    a row here (`links.resolve`). Keyed on immutable ids, never names."""

    __tablename__ = "portal_vendor_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id", ondelete="CASCADE"), unique=True, index=True)
    dealer_id: Mapped[int | None] = mapped_column(default=None, index=True)
    dealer_name: Mapped[str | None] = mapped_column(default=None)  # snapshot, for the report only
    status: Mapped[str] = mapped_column(default=LINK_LINKED)
    source: Mapped[str | None] = mapped_column(default=None)  # the file it came from
    decided_by: Mapped[str | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=now_ist_naive)
    updated_at: Mapped[datetime] = mapped_column(default=now_ist_naive)


class PortalDealer(Base):
    """Every Portal dealer id the Portal itself has offered as a vendor
    option -- the table a manually typed dealer id is checked against."""

    __tablename__ = "portal_dealers"

    dealer_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    dealer_name: Mapped[str | None] = mapped_column(default=None)
    brands: Mapped[list | None] = mapped_column(JSON, default=None)
    odoo_partner_id: Mapped[int | None] = mapped_column(default=None)
    seen_at: Mapped[datetime] = mapped_column(default=now_ist_naive)
