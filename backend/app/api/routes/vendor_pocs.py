"""Vendors page, POC tab (User, 14 Nov 2026): a vendor's people and their
WhatsApp numbers. A POC number IS the vendor -- in his group and one to one
(`registry.lookup` -> `_poc`): his files and typed stock are his stock, his
replies are his quotes.

A POC is an `advance_vendor_contacts` row with `treat_as_vendor = true`. It is
deliberately NOT a registered number: registered numbers each get the 09:30
"share your stock" template and count in the 11:00 summary, and several POCs
of one vendor must not get one each.

Refused, never guessed: our own team's numbers, a number registered to a
different party, and a number that is already another vendor's POC."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders.models import AdvanceVendorContact
from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
from backend.app.integrations.whatsapp.registry import normalize_number
from core.logging_setup import get_logger
from core.models import Vendor

logger = get_logger(__name__)

router = APIRouter(prefix="/api/vendor-pocs", tags=["vendor-pocs"], dependencies=[Depends(get_current_user)])


class PocIn(BaseModel):
    number: str = Field(min_length=6, max_length=20)
    name: str | None = Field(default=None, max_length=80)


def _vendor(vendor_id: int, db: Session) -> Vendor:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vendor not found.")
    return vendor


def _internal(number: str) -> bool:
    try:
        from backend.app.integrations.whatsapp import whapi
        from backend.app.integrations.whatsapp.config import whatsapp_settings

        admins = {normalize_number(a) for a in whatsapp_settings.admin_phone_numbers}
        return number in admins or whapi._is_internal(number) or number in whapi.team_members()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return False


def _refresh_caches() -> None:
    try:
        from backend.app.integrations.whatsapp import whapi

        whapi.forget_cached_groups()
    except Exception:  # noqa: BLE001
        logger.exception("POC: could not refresh the group number cache.")


def _out(vendor: Vendor, db: Session) -> dict:
    registered = db.execute(
        select(WhatsAppRegisteredNumber).where(WhatsAppRegisteredNumber.vendor_id == vendor.id)
    ).scalars().all()
    contacts = db.execute(
        select(AdvanceVendorContact).where(
            AdvanceVendorContact.vendor_id == vendor.id, AdvanceVendorContact.active.is_(True)
        ).order_by(AdvanceVendorContact.id)
    ).scalars().all()
    numbers = [
        {"id": None, "number": r.whatsapp_number, "name": r.note, "kind": "registered",
         "treat_as_vendor": True, "removable": False}
        for r in registered
    ]
    seen = {r.whatsapp_number for r in registered}
    for c in contacts:
        if c.whatsapp_number in seen:
            continue
        numbers.append({
            "id": c.id, "number": c.whatsapp_number, "name": c.name, "source": c.source,
            "kind": "poc" if c.treat_as_vendor else "enquiry",
            "treat_as_vendor": bool(c.treat_as_vendor), "removable": True,
        })
    return {"vendor_id": vendor.id, "vendor_name": vendor.name, "vendor_code": vendor.vendor_code, "numbers": numbers}


@router.get("/vendors")
def list_vendors(db: Session = Depends(get_db)) -> list[dict]:
    """Every active vendor with how many numbers count as him."""
    pocs: dict[int, int] = {}
    for (vid,) in db.execute(select(AdvanceVendorContact.vendor_id).where(
            AdvanceVendorContact.active.is_(True), AdvanceVendorContact.treat_as_vendor.is_(True))):
        pocs[vid] = pocs.get(vid, 0) + 1
    regs: dict[int, int] = {}
    for (vid,) in db.execute(select(WhatsAppRegisteredNumber.vendor_id).where(WhatsAppRegisteredNumber.vendor_id.isnot(None))):
        regs[vid] = regs.get(vid, 0) + 1
    vendors = db.execute(select(Vendor).where(Vendor.active.is_(True)).order_by(Vendor.name)).scalars().all()
    return [{"vendor_id": v.id, "vendor_name": v.name, "vendor_code": v.vendor_code,
             "pocs": pocs.get(v.id, 0), "registered": regs.get(v.id, 0)} for v in vendors]


@router.get("/{vendor_id}")
def get_pocs(vendor_id: int, db: Session = Depends(get_db)) -> dict:
    return _out(_vendor(vendor_id, db), db)


@router.post("/{vendor_id}", status_code=status.HTTP_201_CREATED)
def add_poc(vendor_id: int, body: PocIn, db: Session = Depends(get_db)) -> dict:
    vendor = _vendor(vendor_id, db)
    number = normalize_number(body.number)
    if len(number) < 11 or len(number) > 15:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a valid WhatsApp number (10 digits, or with country code).")
    if _internal(number):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This is one of our own team's numbers -- it cannot be a vendor POC.")
    reg = db.execute(select(WhatsAppRegisteredNumber).where(WhatsAppRegisteredNumber.whatsapp_number == number)).scalar_one_or_none()
    if reg is not None and reg.vendor_id != vendor.id:
        who = db.get(Vendor, reg.vendor_id).name if reg.vendor_id and db.get(Vendor, reg.vendor_id) else "a customer"
        raise HTTPException(status.HTTP_409_CONFLICT, f"{number} is already registered to {who}.")
    other = db.execute(select(AdvanceVendorContact).where(
        AdvanceVendorContact.whatsapp_number == number, AdvanceVendorContact.vendor_id != vendor.id,
        AdvanceVendorContact.active.is_(True), AdvanceVendorContact.treat_as_vendor.is_(True),
    )).scalars().first()
    if other is not None:
        name = db.get(Vendor, other.vendor_id).name if db.get(Vendor, other.vendor_id) else str(other.vendor_id)
        raise HTTPException(status.HTTP_409_CONFLICT, f"{number} is already a POC of {name}.")
    row = db.execute(select(AdvanceVendorContact).where(
        AdvanceVendorContact.vendor_id == vendor.id, AdvanceVendorContact.whatsapp_number == number,
    )).scalar_one_or_none()
    if row is None:
        row = AdvanceVendorContact(vendor_id=vendor.id, whatsapp_number=number, source="POC tab")
        db.add(row)
    row.active = True
    row.treat_as_vendor = True
    if body.name is not None:
        row.name = body.name.strip() or None
    db.commit()
    _refresh_caches()
    logger.info("POC: %s added for vendor %s (%s).", number, vendor.id, vendor.name)
    return _out(vendor, db)


@router.delete("/{vendor_id}/{contact_id}")
def remove_poc(vendor_id: int, contact_id: int, db: Session = Depends(get_db)) -> dict:
    """The number stops counting as this vendor (kept, inactive)."""
    vendor = _vendor(vendor_id, db)
    row = db.get(AdvanceVendorContact, contact_id)
    if row is None or row.vendor_id != vendor.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Number not found for this vendor.")
    row.active = False
    row.treat_as_vendor = False
    db.commit()
    _refresh_caches()
    logger.info("POC: %s removed from vendor %s (%s).", row.whatsapp_number, vendor.id, vendor.name)
    return _out(vendor, db)
