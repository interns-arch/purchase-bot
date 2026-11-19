"""Tez ERP hand-off (17 Nov 2026).

Once a vendor is created and approved, the full vendor details go to Shubham
under the heading "Create vendor on Tez ERP". When he replies OK to that
message the registration is marked "created on Tez ERP" -- the Vendor
creation page shows it in its own column.

  pending      sent, waiting for Shubham's OK
  created      Shubham said OK
  send_failed  the message could not be sent (resend from the dashboard)
  None         not asked (older vendors, or VENDOR_TEZ_NUMBERS not set)

The message is sent on the Meta number ONLY (`whapi.direct_only`): bank
details must never be posted into a WhatsApp group, even when the number is
also a vendor's contact."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.vendor_onboarding import models as m
from backend.app.vendor_onboarding.config import vendor_onboarding_settings as settings
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive as now_ist

logger = get_logger(__name__)

TEZ_PENDING = "pending"
TEZ_CREATED = "created"
TEZ_SEND_FAILED = "send_failed"

HEADING = "Create vendor on Tez ERP"

# "ok", "ok VR-23", "done", "created", "ho gaya" ... (a reply to our message)
_OK = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|done|created|ho\s*gaya|hogaya|kar\s*diya|bana\s*diya|haan|yes)\b|👍)[\s\S]{0,60}$",
    re.IGNORECASE,
)
_CODE = re.compile(r"\bvr\s*[-\s]?\s*(\d+)\b", re.IGNORECASE)


def enabled() -> bool:
    return bool(settings.tez_numbers)


def _sections(request: m.VendorRegistrationRequest) -> list[tuple[str, list[tuple[str, str]]]]:
    """The details in the order of the Tez ERP vendor form: (title, rows)."""
    from backend.app.vendor_onboarding.approvals import _v

    a = request.answers or {}
    gst = request.gst_data or {}
    credit_days = f"{a['credit_days']} days" if a.get("credit_days") not in (None, "", 0, "0") else "0 days (advance)"
    limit = a.get("credit_limit")
    sections = [
        ("🏢 Company", [
            ("GSTIN", _v(a, "gstin")),
            ("PAN", _v(a, "pan")),
            ("Legal name", gst.get("legal_name")),
            ("Type", _v(a, "vendor_type")),
            ("Address", _v(a, "address") or gst.get("address")),
        ]),
        ("📞 Contact", [
            ("Person", _v(a, "contact_person")),
            ("Mobile", _v(a, "mobile")),
            ("Email", _v(a, "email")),
        ]),
        ("🏦 Bank", [
            ("Account holder", _v(a, "account_holder")),
            ("Bank", _v(a, "bank_name")),
            ("Account no.", _v(a, "account_number")),
            ("IFSC", _v(a, "ifsc")),
        ]),
        ("📋 Terms", [
            ("Credit", credit_days + (f", limit ₹{limit}" if limit not in (None, "", 0, "0") else "")),
            ("Dispatch", _v(a, "dispatch_mode")),
            ("Freight", _v(a, "freight_terms")),
            ("Transporter", _v(a, "transporter")),
            ("Lead time", f"{a['lead_time_days']} days" if a.get("lead_time_days") else None),
            ("Min. order", f"₹{a['min_order_value']}" if a.get("min_order_value") else None),
        ]),
        ("🚗 Brands", [
            ("Supplies", _v(a, "brands_supplied")),
            ("Discount", _v(a, "brand_discounts")),
        ]),
    ]
    ref = request.dealer_portal_ref or ""
    if ref:
        sections.append(("🖥️ Dealer Portal", [("Vendor ID", ref.removeprefix("DP-EXISTS-").removeprefix("DP-"))]))
    return [(title, [(k, v) for k, v in rows if v]) for title, rows in sections if any(v for _, v in rows)]


def message_text(request: m.VendorRegistrationRequest) -> str:
    """The WhatsApp message (24-hour window open): headed, one field per line."""
    code = f"VR-{request.id}"
    name = (request.answers or {}).get("vendor_name") or request.vendor_name or "-"
    line = "━━━━━━━━━━━━━━━━━━"
    lines = [f"🆕 *{HEADING}*", f"{code} · *{name}*", line]
    for title, rows in _sections(request):
        lines += ["", f"*{title}*", *(f"{label}: *{value}*" for label, value in rows)]
    lines += ["", line, f"✅ Tez ERP par vendor bana kar is message ko *reply* karke *OK* likhiye ({code})."]
    return chr(10).join(lines)


def template_text(request: m.VendorRegistrationRequest) -> str:
    """The same details for the update template (outside the 24-hour window),
    which allows no new lines: sections split by ' || ', fields by ' | '."""
    code = f"VR-{request.id}"
    name = (request.answers or {}).get("vendor_name") or request.vendor_name or "-"
    parts = [f"🆕 {HEADING} — {code} · {name}"]
    for title, rows in _sections(request):
        parts.append(f"{title}: " + " | ".join(f"{label}: {value}" for label, value in rows))
    parts.append(f"✅ Tez ERP par vendor bana kar is message ko reply karke OK likhiye ({code}).")
    return " || ".join(parts)


def _send(number: str, body: str, flat: str | None = None) -> str | None:
    """Message id of what was sent (the template when he is outside the
    24-hour window, else the text). Meta number only. Raises on failure."""
    from backend.app.integrations.whatsapp import outbound, whapi

    client = WhatsAppClient(whatsapp_settings)
    with whapi.direct_only():
        if not outbound.in_service_window(number) and outbound.template_approved(outbound.UPDATE_TEMPLATE):
            return client.send_template_message(
                number,
                outbound.UPDATE_TEMPLATE,
                whatsapp_settings.template_language,
                [outbound.flatten_for_template(flat or body, limit=1000)],
            )
        return client.send_text_message(number, body)


def send_for(request: m.VendorRegistrationRequest, session: Session, *, force: bool = False) -> bool:
    """Send the details to the Tez numbers. Only once per vendor unless
    `force` (the dashboard's resend). Never raises."""
    try:
        if not enabled():
            return False
        if request.tez_status in (TEZ_PENDING, TEZ_CREATED) and not force:
            return False
        body = message_text(request)
        flat = template_text(request)
        ids: list[str] = []
        for number in settings.tez_numbers:
            try:
                sent = _send(number, body, flat)
                ids.append(sent or "sent")
            except Exception:  # noqa: BLE001
                logger.exception("VR-%s: Tez ERP message to %s failed.", request.id, number)
        # A resend keeps the earlier ids: an OK to either message counts.
        earlier = [x for x in (request.tez_message_id or "").split(",") if x and x != "sent"]
        request.tez_message_id = ",".join(earlier + ids) or None
        request.tez_sent_at = now_ist()
        request.tez_status = TEZ_PENDING if ids else TEZ_SEND_FAILED
        request.tez_done_at = None
        request.tez_done_by = None
        session.flush()
        logger.info("VR-%s: Tez ERP message %s.", request.id, request.tez_status)
        return bool(ids)
    except Exception:  # noqa: BLE001
        logger.exception("VR-%s: could not send the Tez ERP message.", getattr(request, "id", "?"))
        return False


def _pending(session: Session) -> list[m.VendorRegistrationRequest]:
    return list(
        session.execute(
            select(m.VendorRegistrationRequest)
            .where(m.VendorRegistrationRequest.tez_status == TEZ_PENDING)
            .order_by(m.VendorRegistrationRequest.id)
        ).scalars()
    )


def handle_reply(sender: str, text: str | None, reply_to_id: str | None, session: Session) -> list[str] | None:
    """Replies when `sender` (a Tez number) answers OK to one of our messages;
    None when this is not ours, so the message carries on as before.

    Claimed only when the OK is a reply to our message, or names the vendor
    ("OK VR-23"): a bare OK is never taken, because the same number may also
    be answering a vendor question."""
    if sender not in settings.tez_numbers or not _OK.match(text or ""):
        return None
    target = None
    if reply_to_id:
        for request in _pending(session) + _done(session):
            if reply_to_id in (request.tez_message_id or "").split(","):
                target = request
                break
    if target is None:
        code = _CODE.search(text or "")
        if code:
            target = session.get(m.VendorRegistrationRequest, int(code.group(1)))
            if target is not None and target.tez_status not in (TEZ_PENDING, TEZ_CREATED):
                target = None
    if target is None:
        return None
    name = settings.name_of(sender)
    if target.tez_status == TEZ_CREATED:
        return [f"VR-{target.id} ({target.vendor_name}) pehle se Tez ERP par created mark hai."]
    target.tez_status = TEZ_CREATED
    target.tez_done_at = now_ist()
    target.tez_done_by = name
    session.flush()
    logger.info("VR-%s marked created on Tez ERP by %s.", target.id, name)
    try:
        from backend.app.vendor_onboarding import approvals

        approvals._publish("success", f"VR-{target.id} created on Tez ERP", f"{target.vendor_name} — {name}")
    except Exception:  # noqa: BLE001
        pass
    return [f"👍 Noted. VR-{target.id} ({target.vendor_name}) Tez ERP par created mark kar diya."]


def _done(session: Session) -> list[m.VendorRegistrationRequest]:
    return list(
        session.execute(
            select(m.VendorRegistrationRequest)
            .where(m.VendorRegistrationRequest.tez_status == TEZ_CREATED)
            .order_by(m.VendorRegistrationRequest.id.desc())
            .limit(20)
        ).scalars()
    )
