"""Vendor CRUD. Pure business logic -- no print()/input() here."""

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
) -> Vendor:
    name = name.strip()
    if not name:
        raise ValueError("Vendor name cannot be blank.")

    if get_vendor_by_name(name, session) is not None:
        raise ValueError(f"A vendor named '{name}' already exists.")

    vendor = Vendor(name=name, contact_info=contact_info, payment_terms=payment_terms)
    session.add(vendor)
    session.flush()  # assign vendor.id
    return vendor


def get_vendor(vendor_id: int, session: Session) -> Vendor | None:
    return session.get(Vendor, vendor_id)


def get_vendor_by_name(name: str, session: Session) -> Vendor | None:
    return session.execute(
        select(Vendor).where(func.lower(Vendor.name) == name.strip().lower())
    ).scalar_one_or_none()


def list_vendors(session: Session, *, active_only: bool = False) -> list[Vendor]:
    statement = select(Vendor).order_by(Vendor.name)
    if active_only:
        statement = statement.where(Vendor.active.is_(True))
    return list(session.execute(statement).scalars())


def deactivate_vendor(vendor_id: int, session: Session) -> Vendor:
    vendor = get_vendor(vendor_id, session)
    if vendor is None:
        raise LookupError(f"Vendor {vendor_id} not found.")
    vendor.active = False
    session.flush()
    return vendor


def update_vendor(
    vendor_id: int,
    session: Session,
    *,
    name: str | None = None,
    contact_info: str | None = None,
    payment_terms: str | None = None,
    active: bool | None = None,
) -> Vendor:
    """Partial update -- only fields explicitly passed (non-None) are changed.
    Also covers re-enabling a vendor via `active=True` (the counterpart to
    `deactivate_vendor`), so "Edit Vendor" is the one place that toggles it."""
    vendor = get_vendor(vendor_id, session)
    if vendor is None:
        raise LookupError(f"Vendor {vendor_id} not found.")

    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("Vendor name cannot be blank.")
        existing = get_vendor_by_name(name, session)
        if existing is not None and existing.id != vendor_id:
            raise ValueError(f"A vendor named '{name}' already exists.")
        vendor.name = name

    if contact_info is not None:
        vendor.contact_info = contact_info

    if payment_terms is not None:
        vendor.payment_terms = payment_terms

    if active is not None:
        vendor.active = active

    session.flush()
    return vendor
