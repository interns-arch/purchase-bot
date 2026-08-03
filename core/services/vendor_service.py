"""Vendor lookup/creation. There is no manual vendor management in this
application -- vendors are only ever auto-created from an imported
inventory file's name (see `document_processor.dispatcher`). Pure business
logic -- no print()/input() here."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.models import Vendor


def create_vendor(
    name: str,
    session: Session,
    *,
    contact_info: str | None = None,
    payment_terms: str | None = None,
    whatsapp_number: str | None = None,
) -> Vendor:
    name = name.strip()
    if not name:
        raise ValueError("Vendor name cannot be blank.")

    if get_vendor_by_name(name, session) is not None:
        raise ValueError(f"A vendor named '{name}' already exists.")

    vendor = Vendor(
        name=name,
        contact_info=contact_info,
        payment_terms=payment_terms,
        whatsapp_number=whatsapp_number,
    )
    session.add(vendor)
    session.flush()  # assign vendor.id
    return vendor


def get_vendor(vendor_id: int, session: Session) -> Vendor | None:
    return session.get(Vendor, vendor_id)


def get_vendor_by_name(name: str, session: Session) -> Vendor | None:
    return session.execute(
        select(Vendor).where(func.lower(Vendor.name) == name.strip().lower())
    ).scalar_one_or_none()


def get_vendor_by_whatsapp_number(number: str, session: Session) -> Vendor | None:
    number = number.strip()
    if not number:
        return None
    return session.execute(
        select(Vendor).where(Vendor.whatsapp_number == number)
    ).scalar_one_or_none()


