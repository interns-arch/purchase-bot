"""Creating an APPROVED vendor: Dealer Portal first, then ProcureHub (vendor,
code, standing brand discounts/terms, WhatsApp number). Re-runnable: a failed
attempt leaves the request in CREATE_FAILED for a retry from the UI, and a
retry reuses whatever the earlier attempt already created."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.outbound import send_reply_safe
from backend.app.vendor_onboarding import dealer_portal_vendor
from backend.app.vendor_onboarding import models as m
from core.logging_setup import get_logger

logger = get_logger(__name__)


def _terms_text(answers: dict) -> str | None:
    parts = []
    if answers.get("credit_days"):
        parts.append(f"Credit {answers['credit_days']} days")
    if answers.get("credit_limit"):
        parts.append(f"limit ₹{answers['credit_limit']}")
    return ", ".join(parts) or None


def _contact_text(answers: dict) -> str:
    fields = [
        ("Contact", answers.get("contact_person")),
        ("Mobile", answers.get("mobile")),
        ("Email", answers.get("email")),
        ("GSTIN", answers.get("gstin")),
        ("PAN", answers.get("pan")),
        ("Address", answers.get("address")),
    ]
    return " | ".join(f"{label}: {value}" for label, value in fields if value)


def _vendor_whatsapp(request: m.VendorRegistrationRequest) -> str:
    """The VENDOR's WhatsApp number: the chatting number when the vendor
    registered itself, the mobile given in the form when the purchase team
    registered it on the vendor's behalf."""
    answers = request.answers or {}
    if answers.get("_on_behalf") and answers.get("mobile"):
        return f"91{answers['mobile']}"
    return request.whatsapp_number


def _ensure_procurehub_vendor(request: m.VendorRegistrationRequest, session: Session):
    from backend.app.advance_orders.models import DISC_PERCENT, VendorBrand
    from backend.app.integrations.whatsapp import registry
    from core.models import Vendor
    from core.services import vendor_code_service, vendor_service

    answers = request.answers or {}
    vendor = session.get(Vendor, request.vendor_id) if request.vendor_id else None
    if vendor is None:
        # An existing vendor of the same name (e.g. one already sending stock
        # files) is completed rather than duplicated -- the approver was warned.
        vendor = vendor_service.get_vendor_by_name(answers["vendor_name"], session)
    if vendor is None:
        vendor = vendor_service.create_vendor(answers["vendor_name"], session)
    if not vendor.vendor_code:
        vendor.vendor_code = vendor_code_service.generate_vendor_code(vendor.name, session)
    vendor.contact_info = _contact_text(answers)
    vendor.payment_terms = _terms_text(answers) or vendor.payment_terms
    request.vendor_id = vendor.id

    transport = " / ".join(x for x in (answers.get("dispatch_mode"), answers.get("freight_terms")) if x) or None
    for row in answers.get("brand_discounts") or []:
        brand = row["brand"].strip().upper()
        terms = session.execute(
            select(VendorBrand).where(VendorBrand.brand == brand, VendorBrand.vendor_id == vendor.id)
        ).scalar_one_or_none()
        if terms is None:
            terms = VendorBrand(brand=brand, vendor_id=vendor.id)
            session.add(terms)
        terms.discount_type = DISC_PERCENT
        terms.discount_pct = Decimal(str(row["discount_pct"]))
        terms.transport = transport
        terms.payment_terms = _terms_text(answers)

    vendor_number = _vendor_whatsapp(request)
    try:
        registry.register_vendor_number(vendor_number, vendor.id, session, note="vendor registration")
    except Exception as exc:  # noqa: BLE001 -- already registered to someone else: report, don't fail
        logger.warning("%s: could not register %s: %s", request.id, vendor_number, exc)
    session.flush()
    return vendor


def create_vendor_from_request(request: m.VendorRegistrationRequest, session: Session) -> tuple[bool, str]:
    """(ok, detail). Sets the request status to CREATED or CREATE_FAILED."""
    code = f"VR-{request.id}"
    vendor = None
    try:
        with session.begin_nested():
            vendor = _ensure_procurehub_vendor(request, session)
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s: ProcureHub vendor creation failed.", code)
        request.status = m.REG_CREATE_FAILED
        request.dealer_portal_error = f"ProcureHub: {exc}"
        session.flush()
        return False, f"ProcureHub: {exc}"

    if not request.dealer_portal_ref or request.dealer_portal_ref == "DRY-RUN":
        result = dealer_portal_vendor.create_vendor(
            request.answers or {}, vendor_code=vendor.vendor_code, gst=request.gst_data
        )
        if not result.ok:
            request.status = m.REG_CREATE_FAILED
            request.dealer_portal_error = result.error
            session.flush()
            return False, result.error or "Dealer Portal error"
        request.dealer_portal_ref = result.reference
    request.dealer_portal_error = None
    request.status = m.REG_CREATED
    session.flush()

    portal_note = (
        "Dealer Portal: pending (API not connected yet)."
        if request.dealer_portal_ref == "DRY-RUN"
        else f"Dealer Portal ref {request.dealer_portal_ref}."
    )
    if (request.answers or {}).get("_on_behalf"):
        # Tell the purchase-team member who filled it; welcome the vendor too
        # (best effort -- WhatsApp delivers it once the vendor has messaged the bot).
        send_reply_safe(
            request.whatsapp_number,
            f"Vendor {vendor.name} is created. Vendor code: {vendor.vendor_code}. "
            f"Its WhatsApp {_vendor_whatsapp(request)} is registered for stock and ledgers. {portal_note}",
        )
    send_reply_safe(
        _vendor_whatsapp(request),
        f"Congratulations! {vendor.name} is now registered with CarTrends.\n"
        f"Your vendor code: {vendor.vendor_code}\n"
        "You can send your stock list and ledger to this number any time "
        "(send a ledger with the caption LEDGER).",
    )
    logger.info("%s: vendor %s created (%s, %s).", code, vendor.name, vendor.vendor_code, request.dealer_portal_ref)
    return True, f"Created as {vendor.vendor_code}. {portal_note}"
