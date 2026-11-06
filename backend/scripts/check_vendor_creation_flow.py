"""Check: a new vendor registered on WhatsApp all the way to "created"
(30 Oct 2026) -- vendor answers, approver says APPROVE, ProcureHub vendor
made, Dealer Portal creation mail sent, vendor number registered.

Throwaway SQLite; WhatsApp, GST portal, AI and Gmail are all faked -- nothing
is sent:

    .venv\\Scripts\\python.exe -m backend.scripts.check_vendor_creation_flow
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vcreate-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1",
    "VENDOR_ONBOARDING_ENABLED": "true",
    "VENDOR_APPROVER_NUMBERS": "Prateek sir:9999492550,NK Jain sir:9810238966",
    "DEALER_PORTAL_VENDOR_MAIL_TO": "itsupport@cartrends.net",
    # SQLite stamps rows in UTC, 5.5 h behind the IST clock the expiry reads
    # (the live Postgres runs in IST) -- so no expiry inside this check.
    "VENDOR_EMPTY_DRAFT_EXPIRY_HOURS": "48",
})
VENDOR, PRATEEK = "919812345678", "919999492550"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from core.db import get_session, init_db
    from backend.app.vendor_onboarding import approvals, conversation, gst_lookup, service
    from backend.app.vendor_onboarding import models as m

    init_db(force=True)
    sent: list[tuple[str, str]] = []
    mails: list[tuple[str, str, str]] = []
    fake_send = lambda to, body, *a, **k: sent.append((to, body)) or True  # noqa: E731
    for module in (approvals, service, conversation):
        if hasattr(module, "send_reply_safe"):
            module.send_reply_safe = fake_send
    import backend.app.integrations.whatsapp.outbound as outbound

    outbound.send_reply_safe = fake_send
    if hasattr(approvals, "notify"):
        approvals.notify = lambda numbers, body, *a, **k: [sent.append((n, body)) for n in numbers]
    gst_lookup.lookup = lambda gstin: None
    conversation._answer_with_ai = lambda *a, **k: None
    import backend.app.integrations.gmail.mailer as mailer

    mailer.send_email = lambda to, subject, body, *a, **k: mails.append((to, subject, body)) or True

    def say(number, text):
        with get_session() as s:
            if conversation.is_start_command(text) and conversation.open_draft(number, s) is None:
                return conversation.start(number, s, text)
            return conversation.answer(number, text, s)

    print("\n[1] the vendor fills the form")
    replies = say(VENDOR, "new vendor")
    check("'new vendor' starts the form", bool(replies))
    flow = [
        ("27AAPFU0939F1ZV", "GSTIN"),
        ("Test Auto Spares", "name"),
        ("9812345678", "mobile"),
        ("Maruti, Hyundai", "brands"),
        ("Test Auto Spares", "account holder"),
        ("HDFC Bank", "bank"),
        ("50100123456789", "account no."),
        ("HDFC0001234", "IFSC"),
    ]
    for text, what in flow:
        replies = say(VENDOR, text)
        check(f"{what} accepted", bool(replies))
    for _ in range(8):  # optional questions, then the summary
        with get_session() as s:
            step = conversation.open_draft(VENDOR, s).current_step
        if step == conversation.SUMMARY:
            break
        say(VENDOR, "skip all")
    with get_session() as s:
        draft = conversation.open_draft(VENDOR, s)
        check("reaches the summary", draft is not None and draft.current_step == conversation.SUMMARY)
    sent.clear()
    replies = say(VENDOR, "confirm")
    with get_session() as s:
        req = s.query(m.VendorRegistrationRequest).filter_by(whatsapp_number=VENDOR).order_by(m.VendorRegistrationRequest.id.desc()).first()
        status, rid = req.status, req.id
    check("submitted -> waiting for approval", status == m.REG_AWAITING_APPROVAL)
    check("Prateek sir and Jain sir asked to approve", {to for to, _ in sent} >= {PRATEEK, "919810238966"})

    print("\n[2] Jain sir just writes 'Approved'")
    JAIN = "919810238966"
    with get_session() as s:
        other = m.VendorRegistrationRequest(whatsapp_number="919800000000", vendor_name="Other Motors", status=m.REG_AWAITING_APPROVAL, answers={})
        s.add(other)
        s.flush()
        other_id = other.id
    with get_session() as s:
        replies = approvals.handle_reply(JAIN, "Approved", s)
    check("two waiting -> asks which one", replies and f"VR-{rid}" in replies[0] and f"VR-{other_id}" in replies[0])
    with get_session() as s:
        check("nothing approved yet", s.get(m.VendorRegistrationRequest, rid).status == m.REG_AWAITING_APPROVAL)
        s.get(m.VendorRegistrationRequest, other_id).status = m.REG_CANCELLED
    with get_session() as s:
        check("'Approved' from someone who is not an approver is not a decision", approvals.handle_reply("919289015770", "Approved", s) is None)
    sent.clear()
    with get_session() as s:
        replies = approvals.handle_reply(JAIN, "Approved", s)
    print("     ->", (replies or ["-"])[0][:160])
    with get_session() as s:
        req = s.get(m.VendorRegistrationRequest, rid)
        from core.models import Vendor
        from backend.app.integrations.whatsapp import registry

        vendor = s.query(Vendor).filter(Vendor.name.ilike("%test auto spares%")).one_or_none()
        party = registry.lookup(VENDOR, s)
        check("registration CREATED", req.status == m.REG_CREATED)
        check("ProcureHub vendor made with a code", vendor is not None and bool(vendor.vendor_code))
        check("vendor's WhatsApp registered to him", party is not None and vendor is not None and party.party_id == vendor.id)
        check("Dealer Portal reference saved", bool(req.dealer_portal_ref))
    check("creation mail to itsupport@ for the data-entry robot", mails and mails[0][0] == "itsupport@cartrends.net")
    if mails:
        print("     mail subject:", mails[0][1])
    check("vendor told he is registered", any(to == VENDOR and "registered" in body.lower() for to, body in sent))

    print("\n[3] welcome by the UTILITY template")
    from backend.app.integrations.whatsapp.client import WhatsAppClient

    templates: list = []
    real_template = WhatsAppClient.send_template_message
    WhatsAppClient.send_template_message = lambda self, to, name, lang="en", params=None: templates.append((to, name, params)) or "wamid"
    real_window = outbound.in_service_window
    outbound.in_service_window = lambda to: False
    os.environ["VENDOR_WELCOME_TEMPLATE"] = "vendor_registration_complete"
    sent.clear()
    service._welcome("919899104445", "NIRMAL OIL STORE", "CT_NOS", "Congratulations!")
    check("new vendor (never wrote) gets the template with name and code",
          templates == [("919899104445", "vendor_registration_complete", ["NIRMAL OIL STORE", "CT_NOS"])] and not sent)
    outbound.in_service_window = lambda to: True
    templates.clear()
    service._welcome("919899104445", "NIRMAL OIL STORE", "CT_NOS", "Congratulations!")
    check("a vendor who just wrote gets the normal message", not templates and sent == [("919899104445", "Congratulations!")])
    WhatsAppClient.send_template_message, outbound.in_service_window = real_template, real_window

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
