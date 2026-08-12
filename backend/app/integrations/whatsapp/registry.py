"""WhatsApp number registry: the permanent identity layer.

    WhatsApp number  ->  Vendor (or Customer)  ->  Vendor/Customer Code

A file from a registered number is identified by the NUMBER alone -- no
routing command, no caption, no filename convention, no grouping window.
Filenames stay audit metadata; a stray caption can never override a
registered identity. Unregistered numbers are untouched by this module and
keep the existing command/caption flow.

All numbers are normalized to digits-with-country-code (Meta's wa_id shape,
e.g. "919212552626") by `normalize_number` -- registration and lookup both
go through it, so "92569 97173", "+91 92569-97173" and "919256997173" are
the same number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
from core.logging_setup import get_logger
from core.models import Customer, Vendor

logger = get_logger(__name__)

_NON_DIGITS = re.compile(r"\D+")
# India-first default: a bare 10-digit mobile number gets the 91 country
# code (Meta wa_ids always carry one). Anything already 11+ digits is
# assumed to include its country code and is kept as-is.
_DEFAULT_COUNTRY_CODE = "91"


def normalize_number(raw: str | int | None) -> str:
    """Digits-only, with country code: '92569 97173' -> '919256997173'.
    Returns '' for blank input (callers treat that as unregistrable)."""
    digits = _NON_DIGITS.sub("", str(raw or ""))
    if not digits:
        return ""
    digits = digits.lstrip("0")
    if len(digits) == 10:
        return _DEFAULT_COUNTRY_CODE + digits
    return digits


@dataclass(frozen=True)
class RegisteredParty:
    """Lookup result. PLAIN VALUES only (no ORM instances): callers use this
    after the lookup session has closed, where a detached ORM object would
    raise on attribute access."""

    party_type: str  # "vendor" | "customer"
    party_id: int
    name: str


def lookup(sender: str, session: Session) -> RegisteredParty | None:
    """Who a WhatsApp number belongs to, or None if unregistered. The single
    fast-path check the document worker runs on every incoming message."""
    number = normalize_number(sender)
    if not number:
        return None
    row = session.execute(
        select(WhatsAppRegisteredNumber).where(
            WhatsAppRegisteredNumber.whatsapp_number == number
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if row.vendor_id is not None:
        vendor = session.get(Vendor, row.vendor_id)
        if vendor is None:  # orphaned by a raw delete -- treat as unregistered
            return None
        return RegisteredParty("vendor", vendor.id, vendor.name)
    customer = session.get(Customer, row.customer_id)
    if customer is None:
        return None
    return RegisteredParty("customer", customer.id, customer.name)


class NumberAlreadyRegisteredError(ValueError):
    """The number is registered to a DIFFERENT party -- never silently
    re-point a number; the admin must unregister it first (prevents the
    exact wrong-vendor misfiling this registry exists to eliminate)."""

    def __init__(self, number: str, existing: WhatsAppRegisteredNumber):
        self.number = number
        self.existing = existing
        super().__init__(
            f"WhatsApp number {number} is already registered "
            f"(vendor_id={existing.vendor_id}, customer_id={existing.customer_id})."
        )


def _register(
    raw_number: str | int,
    session: Session,
    *,
    vendor_id: int | None = None,
    customer_id: int | None = None,
    note: str | None = None,
) -> WhatsAppRegisteredNumber:
    number = normalize_number(raw_number)
    if not number:
        raise ValueError(f"Not a usable WhatsApp number: {raw_number!r}")
    # The Founder/admin numbers must NEVER be registered as a party: they
    # send files on behalf of many vendors and must keep the caption/command
    # flow.
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    admin_numbers = {
        normalize_number(admin) for admin in whatsapp_settings.admin_phone_numbers
    }
    if number in admin_numbers:
        raise ValueError(
            "This is an admin (Founder) number -- it keeps the caption/command "
            "flow and cannot be registered as a vendor or customer."
        )
    existing = session.execute(
        select(WhatsAppRegisteredNumber).where(
            WhatsAppRegisteredNumber.whatsapp_number == number
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.vendor_id == vendor_id and existing.customer_id == customer_id:
            if note:
                existing.note = note
            return existing  # idempotent re-registration
        raise NumberAlreadyRegisteredError(number, existing)
    row = WhatsAppRegisteredNumber(
        whatsapp_number=number, vendor_id=vendor_id, customer_id=customer_id, note=note
    )
    session.add(row)
    session.flush()
    logger.info(
        "WhatsApp number %s registered (vendor_id=%s, customer_id=%s).",
        number,
        vendor_id,
        customer_id,
    )
    return row


def register_vendor_number(
    raw_number: str | int, vendor_id: int, session: Session, *, note: str | None = None
) -> WhatsAppRegisteredNumber:
    return _register(raw_number, session, vendor_id=vendor_id, note=note)


def register_customer_number(
    raw_number: str | int, customer_id: int, session: Session, *, note: str | None = None
) -> WhatsAppRegisteredNumber:
    return _register(raw_number, session, customer_id=customer_id, note=note)


def unregister(raw_number: str | int, session: Session) -> bool:
    """Remove a number's registration. Returns True if a row was deleted."""
    number = normalize_number(raw_number)
    row = session.execute(
        select(WhatsAppRegisteredNumber).where(
            WhatsAppRegisteredNumber.whatsapp_number == number
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    session.delete(row)
    session.flush()
    logger.info("WhatsApp number %s unregistered.", number)
    return True


@dataclass(frozen=True)
class VendorContact:
    """One registered vendor number, as plain values (usable after the
    session closes -- see `RegisteredParty`)."""

    whatsapp_number: str
    vendor_id: int
    vendor_name: str


def registered_vendor_contacts(session: Session) -> list[VendorContact]:
    """Every registered vendor number -- the audience for the morning stock
    request and reminders. A vendor with several numbers appears once per
    number (each staff number gets the message)."""
    rows = session.execute(
        select(
            WhatsAppRegisteredNumber.whatsapp_number,
            Vendor.id,
            Vendor.name,
        )
        .join(Vendor, WhatsAppRegisteredNumber.vendor_id == Vendor.id)
        .order_by(Vendor.name, WhatsAppRegisteredNumber.whatsapp_number)
    ).all()
    return [VendorContact(number, vendor_id, name) for number, vendor_id, name in rows]


def registered_vendors(session: Session) -> list[tuple[int, str]]:
    """Distinct (vendor_id, name) with at least one registered number -- the
    denominator for the daily participation summary."""
    seen: dict[int, str] = {}
    for contact in registered_vendor_contacts(session):
        seen.setdefault(contact.vendor_id, contact.vendor_name)
    return list(seen.items())
