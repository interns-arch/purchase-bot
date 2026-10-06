"""Tables for vendor onboarding and ledger checks. Shares `core.models.Base`,
so `init_db()` creates them at startup (new tables -- nothing existing is
altered). Timestamps are naive IST, the application-wide convention."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, ForeignKey, Index, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base

# VendorRegistrationRequest.status
REG_DRAFT = "draft"  # the vendor is still answering questions
REG_AWAITING_APPROVAL = "awaiting_approval"
REG_REJECTED = "rejected"
REG_CREATED = "created"  # approved and created in ProcureHub (+ Dealer Portal)
REG_CREATE_FAILED = "create_failed"  # approved, but creation failed -- retry from the UI
REG_EXPIRED = "expired"  # the vendor stopped answering
REG_CANCELLED = "cancelled"

# VendorLedgerSubmission.status
LG_AWAITING_VENDOR = "awaiting_vendor"  # staff sent it; waiting for "which vendor?"
LG_CHECK_FAILED = "check_failed"  # the automatic checks failed; the sender was told
LG_AWAITING_ACCOUNTS = "awaiting_accounts"
LG_PASSED = "passed"  # accounts passed it and it reached the Dealer Portal
LG_PUSH_FAILED = "push_failed"  # accounts passed it; the Dealer Portal push failed
LG_FAILED = "failed"  # accounts failed it; the vendor was asked to correct it


class VendorRegistrationRequest(Base):
    __tablename__ = "vendor_registration_requests"
    __table_args__ = (Index("ix_vendor_reg_number_status", "whatsapp_number", "status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # The number that is filling the form (digits with country code).
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(default=REG_DRAFT, index=True)
    # Key of the question currently being asked; "summary" once every
    # question is answered; None after submission.
    current_step: Mapped[str | None] = mapped_column(default=None)
    # Set while the vendor is changing one answer from the summary, so the
    # chat returns to the summary afterwards instead of the next question.
    editing: Mapped[bool] = mapped_column(default=False)
    # Every form answer, keyed by `questions.Question.key`.
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    # Denormalised for lists and the duplicate check.
    vendor_name: Mapped[str | None] = mapped_column(default=None)
    gstin: Mapped[str | None] = mapped_column(default=None, index=True)
    # The GST portal record (legal name, status, address) -- looked up once.
    gst_data: Mapped[dict | None] = mapped_column(JSON, default=None)

    decided_by: Mapped[str | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    reason: Mapped[str | None] = mapped_column(default=None)

    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL"), default=None
    )
    dealer_portal_ref: Mapped[str | None] = mapped_column(default=None)
    dealer_portal_error: Mapped[str | None] = mapped_column(default=None)

    reminder_sent_at: Mapped[datetime | None] = mapped_column(default=None)
    submitted_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class AiChatTurn(Base):
    """One AI-handled message and what came of it -- the raw material the bot
    learns from (see `learning`). `problems` lists the values the validators
    rejected; `fields` what the AI read; `flow` is "register" or "route"."""

    __tablename__ = "ai_chat_turns"

    id: Mapped[int] = mapped_column(primary_key=True)
    whatsapp_number: Mapped[str] = mapped_column(index=True)
    flow: Mapped[str] = mapped_column()
    user_text: Mapped[str] = mapped_column()
    asked_field: Mapped[str | None] = mapped_column(default=None)
    intent: Mapped[str | None] = mapped_column(default=None)
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    problems: Mapped[list] = mapped_column(JSON, default=list)
    reply: Mapped[str | None] = mapped_column(default=None)
    reviewed: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class AiLesson(Base):
    """A short rule the bot taught itself from reviewing past chats ("'number
    yahi hai' means the sender's own WhatsApp number"). Active lessons are
    added to every AI prompt. They only change how messages are READ and
    WORDED -- never which values are accepted (the validators decide that)."""

    __tablename__ = "ai_lessons"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column()
    applies_to: Mapped[str] = mapped_column(default="all")  # "understand" / "reply" / "route" / "all"
    source: Mapped[str] = mapped_column(default="daily_review")
    active: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class VendorLedgerSubmission(Base):
    __tablename__ = "vendor_ledger_submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    sender: Mapped[str] = mapped_column(index=True)
    vendor_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendors.id", ondelete="SET NULL"), default=None, index=True
    )
    status: Mapped[str] = mapped_column(index=True)
    # A corrected ledger re-sent after a FAIL points at the one it replaces.
    previous_id: Mapped[int | None] = mapped_column(
        ForeignKey("vendor_ledger_submissions.id", ondelete="SET NULL"), default=None
    )
    version: Mapped[int] = mapped_column(default=1)

    file_path: Mapped[str] = mapped_column()
    original_filename: Mapped[str | None] = mapped_column(default=None)

    # What was read off the ledger.
    ledger_vendor_name: Mapped[str | None] = mapped_column(default=None)
    ledger_gstin: Mapped[str | None] = mapped_column(default=None)
    party_name: Mapped[str | None] = mapped_column(default=None)
    period_from: Mapped[date | None] = mapped_column(default=None)
    period_to: Mapped[date | None] = mapped_column(default=None)
    opening_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    opening_side: Mapped[str | None] = mapped_column(default=None)  # "Dr" / "Cr"
    debit_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    credit_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    closing_balance: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), default=None)
    closing_side: Mapped[str | None] = mapped_column(default=None)
    lines: Mapped[list] = mapped_column(JSON, default=list)
    # [{"name": ..., "ok": bool, "detail": ...}]
    checks: Mapped[list] = mapped_column(JSON, default=list)
    read_by: Mapped[str | None] = mapped_column(default=None)  # "excel" / "pdf-text" / AI provider

    decided_by: Mapped[str | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    reason: Mapped[str | None] = mapped_column(default=None)
    dealer_portal_ref: Mapped[str | None] = mapped_column(default=None)
    dealer_portal_error: Mapped[str | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
