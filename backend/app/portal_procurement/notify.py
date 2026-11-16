"""The PO PDF and its summary, to the purchase user (brief, section 10).

The PDF goes ONLY to the configured purchase users (PORTAL_PROCUREMENT_NOTIFY_
NUMBERS, else the admin numbers + purchase team) -- never to a vendor. They
review it and share it with the vendor team themselves.

WhatsApp only lets a document through inside the 24-hour window. A purchase
user outside it gets the summary as the approved update template, and the
PDF itself the next time they write anything to the bot (`deliver_pending`,
called from the incoming-text handler)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.portal_procurement import models as m
from backend.app.portal_procurement.client import PortalClient, PortalError
from backend.app.portal_procurement.config import settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

PDF_DIR = Path(os.environ.get("PORTAL_PROCUREMENT_PDF_DIR") or "uploads/portal_po")


def _norm(value) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _pdf_path(po: m.PortalPoMaster) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in po.po_mst)
    return PDF_DIR / f"{safe}.pdf"


def _allocations(session: Session, po: m.PortalPoMaster) -> list[m.PortalAllocation]:
    return list(session.execute(
        select(m.PortalAllocation).where(m.PortalAllocation.id.in_(po.allocation_ids or [0]))
    ).scalars().all())


def _pdf_text(content: bytes) -> str:
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages[:20])


def fetch_and_check_pdf(session: Session, po: m.PortalPoMaster, client: PortalClient):
    """(ok, why_not, sha256, size). Saves the PDF only when every check passes."""
    ref = po.odoo_ref or po.odoo_name
    if not ref:
        return False, "no Odoo reference", None, None
    try:
        response = client.po_pdf(str(ref))
    except PortalError as exc:
        return False, f"download failed: HTTP {exc.status}", None, None
    content = response.content or b""
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}", None, None
    if "application/pdf" not in response.headers.get("content-type", "").lower():
        return False, f"not a PDF ({response.headers.get('content-type', '-')})", None, None
    if not content.startswith(b"%PDF"):
        return False, "payload does not start with %PDF", None, None
    if len(content) < settings.min_pdf_bytes:
        return False, f"PDF too small ({len(content)} bytes)", None, None
    try:
        text = _norm(_pdf_text(content))
    except Exception:  # noqa: BLE001
        logger.exception("Portal procurement: could not read the PDF text for %s.", po.po_mst)
        return False, "PDF text could not be read", None, None
    if not text:
        return False, "PDF has no readable text", None, None
    # The PDF must be THIS PO: its Odoo reference and every part of it.
    names = [n for n in (po.odoo_name, po.odoo_ref) if n]
    if names and not any(_norm(n) in text for n in names):
        return False, f"Odoo reference {names[0]} not found in the PDF", None, None
    from backend.app.portal_procurement.models import PortalDemandLine

    for alloc in _allocations(session, po):
        demand = session.get(PortalDemandLine, alloc.demand_line_id)
        if demand is not None and _norm(demand.part_no) not in text:
            return False, f"part {demand.part_no} not found in the PDF", None, None
    digest = hashlib.sha256(content).hexdigest()
    path = _pdf_path(po)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True, None, digest, len(content)


def summary_text(session: Session, po: m.PortalPoMaster) -> str:
    from backend.app.portal_procurement.models import PortalDemandLine
    from backend.app.portal_procurement.service import _vendor_name

    allocs = _allocations(session, po)
    groups: list[str] = []
    rows: list[str] = []
    vendor = po.vendor_name
    for alloc in allocs:
        demand = session.get(PortalDemandLine, alloc.demand_line_id)
        if demand is None:
            continue
        if demand.source_po_group_id and demand.source_po_group_id not in groups:
            groups.append(demand.source_po_group_id)
        rate = f"₹{alloc.quoted_rate_ex_gst:.2f} ex-GST" if alloc.quoted_rate_ex_gst is not None else "rate -"
        tat = f"{alloc.quoted_tat_days} din" if alloc.quoted_tat_days is not None else "TAT -"
        rows.append(f"• {demand.part_no} x{alloc.quantity} — {rate}, {tat} (quote {alloc.quote_reference})")
        vendor = vendor or _vendor_name(alloc.vendor_id, session)
    return (
        f"📄 Purchase order ready\n"
        f"PO-MST: {po.po_mst}\n"
        f"Odoo PO: {po.odoo_name or po.odoo_ref}\n"
        f"Vendor: {vendor or po.vendor_dealer_id}\n"
        + "\n".join(rows)
        + "\n\nReview this PO before sharing it with the vendor team."
    )


def _send_pdf(number: str, po: m.PortalPoMaster, caption: str, media_cache: dict) -> str | None:
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    client = WhatsAppClient(whatsapp_settings)
    filename = f"{po.odoo_name or po.po_mst}.pdf".replace("/", "-")
    if "id" not in media_cache:
        media_cache["id"] = client.upload_media(_pdf_path(po).read_bytes(), filename, "application/pdf")
    return client.send_document_message(number, media_cache["id"], filename, caption[:1000]) or "sent"


def send_po(session: Session, po: m.PortalPoMaster) -> bool:
    """True once every purchase user has the PDF or (outside the 24 h window)
    the summary with the PDF waiting for their next message."""
    from backend.app.integrations.whatsapp import outbound, registry
    from backend.app.portal_procurement.service import _numbers

    if not _pdf_path(po).exists():
        return False
    caption = summary_text(session, po)
    notified = dict(po.notified or {})
    pending = list(po.notify_pending or [])
    media: dict = {}
    reached = False
    for raw in _numbers(settings.notify_numbers, session):
        number = registry.normalize_number(raw)
        if not number or number in notified:
            reached = reached or number in notified
            continue
        try:
            if outbound.in_service_window(number):
                notified[number] = _send_pdf(number, po, caption, media)
                if number in pending:
                    pending.remove(number)
                reached = True
            elif number not in pending:
                if not outbound.template_approved(outbound.UPDATE_TEMPLATE):
                    logger.warning("Portal procurement: %s is outside the 24 h window and the update "
                                   "template is not approved -- PDF waits for their next message.", number)
                else:
                    from backend.app.integrations.whatsapp.client import WhatsAppClient
                    from backend.app.integrations.whatsapp.config import whatsapp_settings

                    WhatsAppClient(whatsapp_settings).send_template_message(
                        number, outbound.UPDATE_TEMPLATE, whatsapp_settings.template_language,
                        [outbound.flatten_for_template(
                            caption + "\nPDF ke liye is chat mein koi bhi message bhejiye.")],
                    )
                pending.append(number)
                reached = True
            else:
                reached = True
        except Exception:  # noqa: BLE001 -- the next number is tried; retried next step
            logger.exception("Portal procurement: PO %s to %s failed.", po.po_mst, number)
        # Persist after every number so a crash never sends the same PDF twice.
        po.notified = dict(notified) or None
        po.notify_pending = list(pending) or None
        session.flush()
        session.commit()
    return reached


def deliver_pending(sender: str) -> None:
    """A purchase user wrote to the bot: their window is open, send every PO
    PDF that was waiting for them. Never claims the message."""
    try:
        from backend.app.integrations.whatsapp import registry
        from backend.app.portal_procurement.config import settings as cfg
        from core.db import get_session

        if not cfg.enabled:
            return
        number = registry.normalize_number(sender)
        if not number:
            return
        with get_session() as session:
            for po in session.execute(
                select(m.PortalPoMaster).where(m.PortalPoMaster.notify_pending.is_not(None))
            ).scalars().all():
                if number not in (po.notify_pending or []) or not _pdf_path(po).exists():
                    continue
                notified = dict(po.notified or {})
                notified[number] = _send_pdf(number, po, summary_text(session, po), {})
                po.notified = notified
                po.notify_pending = [n for n in po.notify_pending if n != number] or None
                session.commit()
                logger.info("Portal procurement: waiting PO PDF %s delivered to %s.", po.po_mst, number)
    except Exception:  # noqa: BLE001 -- never break the incoming-text handling
        logger.exception("Portal procurement: delivering a waiting PO PDF failed.")
