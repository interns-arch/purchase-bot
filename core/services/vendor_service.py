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


def list_vendors(session: Session) -> list[Vendor]:
    """Every vendor in the database, ordered by vendor code then name --
    used by read-only outputs (e.g. the consolidated Vendor Inventory
    workbook) that need to enumerate all vendors, not just one."""
    return list(
        session.execute(
            select(Vendor).order_by(func.coalesce(Vendor.vendor_code, Vendor.name), Vendor.name)
        ).scalars()
    )


import re as _re

# Filler words that do NOT distinguish one vendor from another (the Founder's
# rule: "Bijwasan" and "Bijwasan Stock" are the SAME entity). Only generic
# stock-file words are listed -- real name words are never stripped, so
# distinct vendors (aman vs amit) can never merge.
_VENDOR_NAME_FILLERS = {"stock", "stocks", "inventory", "stocklist"}


def normalise_vendor_name(name: str) -> str:
    """Identity form of a vendor name: lowercase, alphanumeric words only,
    with generic filler words ('stock' etc.) removed. 'BIJWASHAN STOCK' ->
    'bijwashan'; 'Delhi Branch Stock' -> 'delhi branch'."""
    words = _re.split(r"[^a-z0-9]+", (name or "").strip().lower())
    kept = [word for word in words if word and word not in _VENDOR_NAME_FILLERS]
    return " ".join(kept) or (name or "").strip().lower()


def get_vendor_by_name(name: str, session: Session) -> Vendor | None:
    """Match by name, tolerating case and generic filler words: a file
    captioned 'BIJWASHAN STOCK' reuses the existing 'Bijwasan Stock' /
    'BIJWASHAN' vendor instead of onboarding a duplicate. Exact
    (case-insensitive) match wins first; the filler-insensitive match only
    runs when that finds nothing."""
    exact = session.execute(
        select(Vendor).where(func.lower(Vendor.name) == name.strip().lower())
    ).scalar_one_or_none()
    if exact is not None:
        return exact

    wanted = normalise_vendor_name(name)
    if not wanted:
        return None
    for vendor in session.execute(select(Vendor)).scalars():
        if normalise_vendor_name(vendor.name) == wanted:
            return vendor

    # REMEMBERED aliases (Founder's rule): a name that once belonged to a
    # merged-away duplicate (e.g. 'BIJWASHAN STOCK' after merging into
    # 'Bijvasan') resolves to the vendor it was merged into -- forever.
    from core.models import VendorNameAlias

    alias = session.execute(
        select(VendorNameAlias).where(VendorNameAlias.normalized_name == wanted)
    ).scalar_one_or_none()
    if alias is not None:
        return session.get(Vendor, alias.vendor_id)
    return None


def get_vendor_by_whatsapp_number(number: str, session: Session) -> Vendor | None:
    number = number.strip()
    if not number:
        return None
    return session.execute(
        select(Vendor).where(Vendor.whatsapp_number == number)
    ).scalar_one_or_none()


