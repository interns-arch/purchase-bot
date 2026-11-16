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
        ("haan", "also add as customer (Haan)"),
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

    print("\n[4] Dealer Portal vendor/create API (admin login), HTTP faked")
    _check_portal_api()

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = str(body)

    def json(self):
        return self._body


def _check_portal_api() -> None:
    from backend.app.vendor_onboarding import dealer_portal_vendor as dpv
    from backend.app.vendor_onboarding import portal_vendor_api as api
    from backend.app.vendor_onboarding import validators as v

    check("haan/nahi: 'nhi' -> Nahi, '1' -> Haan, 'maybe' refused",
          v.haan_nahi("nhi")[0] == "Nahi" and v.haan_nahi("1")[0] == "Haan" and v.haan_nahi("maybe")[0] is None)
    api.settings.username, api.settings.password = "admin_test", "secret"
    calls: list[tuple[str, str, dict | None]] = []
    portal = {"dealers": [{"dealer_id": 5, "dealer_name": "Old Motors", "gst_no": "07AAAAA0000A1Z5", "phone": "9000000001"}],
              "users": [{"user_id": 9, "username": "test_auto_spares", "phone": "9000000002"}]}

    def post(url, json=None, headers=None, timeout=None):
        calls.append(("POST", url, json))
        if url.endswith("/auth/login"):
            return _Resp(200, {"access_token": "tok"})
        if url.endswith("/users/vendor/create"):
            return _Resp(200, {"message": "ok", "account_id": 8700})
        return _Resp(200, {})

    def get(url, headers=None, timeout=None, follow_redirects=None):
        calls.append(("GET", url, None))
        return _Resp(200, portal["dealers"] if "/dealers/" in url else portal["users"])

    api.httpx.post, api.httpx.get = post, get
    answers = {"vendor_name": "Test Auto Spares", "gstin": "27AAPFU0939F1ZV", "mobile": "9812345678",
               "also_customer": "Haan", "credit_days": "30", "address": "12 Ring Road, Lajpat Nagar, Delhi"}
    result = dpv.create_vendor(answers, vendor_code="CT_TAS", gst=None)
    create = next((c[2] for c in calls if c[1].endswith("/users/vendor/create")), None)
    check("new vendor -> vendor/create called, ref DP-8700", result.ok and result.reference == "DP-8700" and create is not None)
    check("also_add_as_customer true for Haan", create and create["also_add_as_customer"] is True)
    check("taken username skipped (test_auto_spares is a login already)", create and create["username"] != "test_auto_spares")
    check("phone 10 digits, gst_no, credit_days int", create and create["phone"] == "9812345678"
          and create["gst_no"] == "27AAPFU0939F1ZV" and create["credit_days"] == 30)
    check("logged out after", calls[-1][1].endswith("/auth/logout"))

    calls.clear()
    result = dpv.create_vendor({**answers, "gstin": "07AAAAA0000A1Z5", "also_customer": "Nahi"}, vendor_code="X", gst=None)
    check("same GSTIN on the Portal -> NOT created, existing reported",
          result.ok and result.reference == "DP-EXISTS-5" and not any(c[1].endswith("/vendor/create") for c in calls))
    calls.clear()
    result = dpv.create_vendor({**answers, "mobile": "9000000002"}, vendor_code="X", gst=None)
    check("same mobile on the Portal -> NOT created",
          result.ok and result.reference == "DP-EXISTS-9" and not any(c[1].endswith("/vendor/create") for c in calls))

    def get_down(url, **_k):
        raise RuntimeError("timeout")

    api.httpx.get = get_down
    calls.clear()
    result = dpv.create_vendor(answers, vendor_code="X", gst=None)
    check("Portal lists unreadable -> not created, request fails for a retry",
          not result.ok and not any(c[1].endswith("/vendor/create") for c in calls))
    api.settings.username = api.settings.password = ""


if __name__ == "__main__":
    sys.exit(main())
