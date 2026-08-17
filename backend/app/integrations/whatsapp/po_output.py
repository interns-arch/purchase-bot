"""Send each generated Purchase Order on WhatsApp -- the Founder's
distribution rule, verbatim from his clarification:

    "There should be three WhatsApp recipients: me, the person who
     originally sent the order via WhatsApp, and the vendor's registered
     number. If the vendor's number is not available, then it goes to two."

So, per PO (deduplicated):
  1. the VENDOR's registered number(s) -- and a vendor only ever receives
     THEIR OWN PO ("It shouldn't happen that Anuj also receives Vinay's
     PO"), which holds by construction: POs are generated one-per-vendor
     and each send is addressed per-PO;
  2. every registered PURCHASE-TEAM member (`register team` flow);
  3. the WhatsApp number that ORIGINALLY SENT the customer order file,
     when the order arrived over WhatsApp;
  4. the Founder/admin number(s).

`WHATSAPP_SEND_PO=false` disables all of it;
`WHATSAPP_SEND_PO_TO_VENDOR=false` keeps it internal (team + founder only).

Caveat worth knowing: WhatsApp only allows free-form document sends to
numbers that messaged the bot within 24h. A vendor who submitted stock this
morning has the window open -- the normal case, since POs follow the day's
stock. A send outside the window fails for that recipient only; it is
logged and reported on the PO's toast, never raised."""

from __future__ import annotations

import io

from sqlalchemy import select

from backend.app.integrations.whatsapp import outbound, registry
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.models import (
    PurchaseTeamMember,
    WhatsAppRegisteredNumber,
)
from backend.app.notifications import broker
from core.logging_setup import get_logger

logger = get_logger(__name__)

XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _order_sender_number(customer_order_id: int, session) -> str | None:
    """The WhatsApp number that submitted this customer order's file, if it
    arrived over WhatsApp."""
    from backend.app.documents.models import IncomingDocument

    document = session.execute(
        select(IncomingDocument)
        .where(
            IncomingDocument.customer_order_id == customer_order_id,
            IncomingDocument.sender.isnot(None),
        )
        .order_by(IncomingDocument.id.desc())
    ).scalars().first()
    if document is None or not document.sender:
        return None
    number = registry.normalize_number(document.sender)
    return number or None


def po_recipients(po, session) -> tuple[list[str], bool]:
    """(deduplicated recipient numbers, vendor_included) for one PO."""
    numbers: list[str] = []

    vendor_included = False
    if whatsapp_settings.send_po_to_vendor:
        vendor_numbers = list(session.execute(
            select(WhatsAppRegisteredNumber.whatsapp_number).where(
                WhatsAppRegisteredNumber.vendor_id == po.vendor_id
            )
        ).scalars())
        if vendor_numbers:
            vendor_included = True
            numbers.extend(vendor_numbers)

    numbers.extend(session.execute(select(PurchaseTeamMember.whatsapp_number)).scalars())

    sender = _order_sender_number(po.customer_order_id, session)
    if sender:
        numbers.append(sender)

    numbers.extend(
        registry.normalize_number(admin) for admin in whatsapp_settings.admin_phone_numbers
    )

    deduped: list[str] = []
    for number in numbers:
        if number and number not in deduped:
            deduped.append(number)
    return deduped, vendor_included


def send_po_on_whatsapp(po_id: int, session) -> bool:
    """Deliver one PO's workbook to its recipients, reading through the
    CALLER'S session (the POs may not be committed yet -- the same pattern
    the internal PO email uses). Best-effort, never raises."""
    try:
        if not whatsapp_settings.send_po:
            return False

        from backend.app.core.config import settings
        from core.models import Vendor, VendorPurchaseOrder
        from core.services import purchase_order_generation_service as po_service

        po = session.get(VendorPurchaseOrder, po_id)
        if po is None:
            return False
        vendor = session.get(Vendor, po.vendor_id)
        lines = po_service.list_purchase_order_lines(po_id, session)
        workbook = po_service.to_export_workbook(
            po,
            lines,
            company_name=settings.company_name,
            company_address=settings.company_address,
            company_phone=settings.company_phone,
            company_email=settings.company_email,
        )
        recipients, vendor_included = po_recipients(po, session)
        po_number = po.po_number
        vendor_name = vendor.name if vendor else "-"
        order_id = po.customer_order_id
        total_qty = sum((float(line.quantity) for line in lines), 0.0)

        if not recipients:
            logger.info("PO %s: no WhatsApp recipients configured -- skipped.", po_number)
            return False

        buffer = io.BytesIO()
        workbook.save(buffer)
        content = buffer.getvalue()
        caption = (
            f"📋 Purchase Order {po_number}\n"
            f"Vendor: {vendor_name}\n"
            f"Customer Order: {order_id}\n"
            f"{len(lines)} line(s), qty {total_qty:g}"
        )

        sent_to: list[str] = []
        failed = 0
        for to in recipients:
            if outbound.send_document_safe(
                to, content, f"{po_number}.xlsx", XLSX_MIME_TYPE, caption
            ):
                sent_to.append(to)
            else:
                failed += 1

        detail = (
            f"Vendor: {vendor_name}"
            + (" (vendor number included)" if vendor_included else " (vendor number NOT registered)")
            + f"\nRecipients reached: {len(sent_to)} of {len(recipients)}"
        )
        if failed:
            detail += f"\n⚠️ {failed} send(s) failed (24h window / delivery — see logs)."
        broker.publish(
            "success" if sent_to else "warning",
            f"PO {po_number} sent on WhatsApp." if sent_to
            else f"PO {po_number} could not be sent on WhatsApp.",
            detail,
            mirror=False if sent_to else True,
        )
        return bool(sent_to)
    except Exception:  # noqa: BLE001 -- PO delivery must never affect generation
        logger.exception("WhatsApp PO delivery failed for po_id=%s.", po_id)
        return False
