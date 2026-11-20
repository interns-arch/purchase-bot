"""Check: the vendor approval card (12 Nov 2026) -- full details grouped
for a phone, APPROVE / REJECT reply buttons, a tap decides, Reject asks for
the reason first. Throwaway SQLite; WhatsApp, Portal and Gmail are faked.

    .venv\\Scripts\\python.exe -m backend.scripts.check_vendor_approval_buttons
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vbtn-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "1",
    "VENDOR_ONBOARDING_ENABLED": "true",
    "VENDOR_APPROVER_NUMBERS": "Prateek sir:9999492550,NK Jain sir:9810238966",
    "VENDOR_APPROVAL_TEMPLATE": "vendor_approval_request",
    "DEALER_PORTAL_VENDOR_MAIL_TO": "itsupport@cartrends.net",
})
PRATEEK, JAIN = "919999492550", "919810238966"
failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from core.db import get_session, init_db
    from backend.app.integrations.whatsapp import outbound, parser
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.vendor_onboarding import approvals, service
    from backend.app.vendor_onboarding import models as m
    import backend.app.advance_orders.models  # noqa: F401 -- vendor_brands table

    init_db(force=True)
    sent: list[tuple[str, str]] = []
    cards: list[tuple[str, str, list, str | None]] = []
    templates: list[tuple[str, str]] = []
    fake_send = lambda to, body, *a, **k: sent.append((to, body)) or True  # noqa: E731
    approvals.send_reply_safe = service.send_reply_safe = outbound.send_reply_safe = fake_send
    WhatsAppClient.send_button_message = (
        lambda self, to, body, buttons, header=None, footer=None: cards.append((to, body, buttons, footer)) or "wamid"
    )
    WhatsAppClient.send_template_message = lambda self, to, name, lang="en", params=None: templates.append((to, name)) or "wamid"
    window = {"open": True}
    outbound.in_service_window = lambda to: window["open"]
    import backend.app.integrations.gmail.mailer as mailer

    mailer.send_email = lambda *a, **k: True
    from backend.app.integrations.whatsapp import vendor_group

    vendor_group.create_for_vendor_in_background = lambda *a, **k: None

    answers = {
        "gstin": "07BFMPK7169C1Z8", "vendor_name": "OM SAI RAM ENTERPRISES", "mobile": "9810541225",
        "brands_supplied": "Ford", "account_holder": "OM SAI RAM ENTERPRISES", "bank_name": "HDFC Bank",
        "account_number": "50100123456789", "ifsc": "HDFC0001234", "also_customer": "Nahi",
        "brand_discounts": [{"brand": "FORD", "discount_pct": 8}], "credit_days": 30,
        "_on_behalf": True, "_created_by": "Seema",
    }

    def new_request() -> int:
        with get_session() as s:
            r = m.VendorRegistrationRequest(
                whatsapp_number="919289015770", vendor_name=answers["vendor_name"], gstin=answers["gstin"],
                status=m.REG_AWAITING_APPROVAL, answers=dict(answers),
                gst_data={"legal_name": "RAM KUMAR", "status": "Active", "registration_date": "01/04/2019"},
            )
            s.add(r)
            s.flush()
            approvals.request_vendor_approval(r, s)
            return r.id

    print("\n[1] the card")
    rid = new_request()
    check("one card with buttons to each approver", {c[0] for c in cards} == {PRATEEK, JAIN} and not templates)
    to, body, buttons, footer = cards[0]
    print("\n" + body + f"\n   [{buttons[0][1]}]  [{buttons[1][1]}]   ({footer})\n")
    check("buttons Approve / Reject carry the VR id",
          [b[0] for b in buttons] == [f"VRBTN APPROVE {rid}", f"VRBTN REJECT {rid}"])
    check("details grouped: company, contact, brands, bank, terms, portal",
          all(t in body for t in ("Company", "Contact", "Brands", "Bank", "Terms", "Dealer Portal")))
    check("values shown: GSTIN, GST check, bank a/c, FORD 8%, customer Nahi, filled by Seema",
          all(t in body for t in ("07BFMPK7169C1Z8", "Active", "50100123456789", "FORD 8", "Nahi", "Seema")))
    check("greets Prateek sir", body.startswith("Namaste Prateek sir"))
    check("fits one WhatsApp card (<=1024)", len(body) <= 1024)

    print("\n[2] a tap comes in as text")
    payload = {"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "1"}, "messages": [{
        "from": PRATEEK, "id": "wamid.x", "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": f"VRBTN APPROVE {rid}", "title": "✅ Approve"}}}]}}]}]}
    texts = parser.parse_text_messages(payload)
    check("button_reply -> 'VRBTN APPROVE n'", [t.text for t in texts] == [f"VRBTN APPROVE {rid}"])

    print("\n[3] Approve tapped")
    sent.clear()
    with get_session() as s:
        replies = approvals.handle_reply(PRATEEK, f"VRBTN APPROVE {rid}", s)
    print("     ->", (replies or ["-"])[0][:150])
    with get_session() as s:
        check("approved and created", s.get(m.VendorRegistrationRequest, rid).status == m.REG_CREATED)
    with get_session() as s:
        again = approvals.handle_reply(JAIN, f"VRBTN APPROVE {rid}", s)
    check("Jain sir tapping later is told it is already decided", again and "already decided" in again[0])

    print("\n[4] Reject tapped -> reason asked -> reason rejects")
    cards.clear()
    rid2 = new_request()
    with get_session() as s:
        ask = approvals.handle_reply(PRATEEK, f"VRBTN REJECT {rid2}", s)
    check("asks for the reason, not rejected yet", ask and "reason" in ask[0].lower())
    with get_session() as s:
        check("still waiting", s.get(m.VendorRegistrationRequest, rid2).status == m.REG_AWAITING_APPROVAL)
    sent.clear()
    with get_session() as s:
        done = approvals.handle_reply(PRATEEK, "GST cancelled hai", s)
    with get_session() as s:
        r2 = s.get(m.VendorRegistrationRequest, rid2)
        check("rejected with his reason", r2.status == m.REG_REJECTED and r2.reason == "GST cancelled hai")
    check("requester told with the reason", any("GST cancelled hai" in b for _t, b in sent))

    print("\n[5] Reject tapped by mistake -> CANCEL")
    rid3 = new_request()
    with get_session() as s:
        approvals.handle_reply(PRATEEK, f"VRBTN REJECT {rid3}", s)
        cancel = approvals.handle_reply(PRATEEK, "cancel", s)
    with get_session() as s:
        check("CANCEL keeps it waiting", s.get(m.VendorRegistrationRequest, rid3).status == m.REG_AWAITING_APPROVAL
              and cancel and "reject nahi" in cancel[0])

    print("\n[6] outside the 24-hour window -> template + text, as before")
    cards.clear(); templates.clear(); sent.clear()
    window["open"] = False
    new_request()
    check("no button card, template to both approvers", not cards and {t[0] for t in templates} == {PRATEEK, JAIN})
    check("details still sent as text with typed replies", any("APPROVE VR-" in b and "Bank" in b for _t, b in sent))
    out_of_window = max(r for r in (rid, rid2, rid3) if r) + 1  # the request made just above

    print("\n[6b] 20 Nov (VR-26): approver says 'hi' after the template -> the full card comes now")
    cards.clear(); sent.clear()
    window["open"] = True  # he wrote, so his window is open
    with get_session() as s:
        hi = approvals.handle_reply(PRATEEK, "Hi", s)
    check("'Hi' is answered by the card, not the help menu", hi == [])
    check("...full details with Approve / Reject buttons, to him only",
          len(cards) == 1 and cards[0][0] == PRATEEK and "Bank" in cards[0][1]
          and any(f"APPROVE {out_of_window}" in b[0] for b in cards[0][2]))
    cards.clear()
    with get_session() as s:
        again = approvals.handle_reply(PRATEEK, "hi", s)
    check("a second 'hi' does not repeat the card", again is None and not cards)
    with get_session() as s:
        other = approvals.handle_reply(JAIN, "kal baat karte hain", s)
    check("Jain sir's first message also brings his card, and the text goes on to the normal handlers",
          other is None and len(cards) == 1 and cards[0][0] == JAIN)
    cards.clear()
    with get_session() as s:
        done = approvals.handle_reply(PRATEEK, f"APPROVE VR-{out_of_window}", s)
    check("APPROVE still works directly", bool(done) and not cards)

    print("\n[7] dashboard RESEND of an OLD waiting request, then Approve -> Portal API")
    window["open"] = True
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api.routes import vendor_registrations as vr
    from backend.app.auth.dependencies import get_current_user
    from backend.app.vendor_onboarding import portal_vendor_api as api

    app = FastAPI()
    app.include_router(vr.router)
    app.dependency_overrides[get_current_user] = lambda: None
    web = TestClient(app)
    old = {k: v for k, v in answers.items() if k != "also_customer"}  # filled before the question existed
    with get_session() as s:
        r = m.VendorRegistrationRequest(
            whatsapp_number="919289015770", vendor_name="HINDUSTAN PETROLEUM PRODUCT AND LUBRICANT",
            gstin="07AGEPG4675R1ZR", status=m.REG_AWAITING_APPROVAL,
            answers={**old, "vendor_name": "HINDUSTAN PETROLEUM PRODUCT AND LUBRICANT", "gstin": "07AGEPG4675R1ZR",
                     "mobile": "9811111111", "brand_discounts": []},
        )
        s.add(r)
        s.flush()
        old_id = r.id
    listed = web.get("/api/vendor-registrations").json()
    row = next(i for i in listed["items"] if i["id"] == old_id)
    check("tab shows it WAITING and 'customer not asked'", row["stage"] == "WAITING" and row["customer_asked"] is False)
    refused = web.post(f"/api/vendor-registrations/{old_id}/resend", json={})
    check("resend without Haan/Nahi is refused (422)", refused.status_code == 422)
    cards.clear(); templates.clear(); sent.clear()
    ok = web.post(f"/api/vendor-registrations/{old_id}/resend", json={"also_customer": "Haan"})
    check("resend with Haan -> 200, sent to both approvers", ok.status_code == 200 and "Prateek sir" in ok.json()["sent_to"])
    check("reminder card with buttons to both", {c[0] for c in cards} == {PRATEEK, JAIN}
          and all("Reminder" in c[1] and c[2][0][0] == f"VRBTN APPROVE {old_id}" for c in cards))
    check("card shows 'Also add as customer: Haan'", all("Also add as customer: Haan" in c[1] for c in cards))
    again = web.post(f"/api/vendor-registrations/{old_id}/resend", json={})
    check("second resend within 10 min refused (429)", again.status_code == 429)
    not_waiting = web.post(f"/api/vendor-registrations/{rid}/resend", json={})
    check("resend of an approved request refused (409)", not_waiting.status_code == 409)

    # Approve tapped -> the Portal vendor/create API (faked) with the admin login.
    api.settings.username, api.settings.password = "admin_test", "secret"
    calls: list = []

    class _R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, str(body)

        def json(self):
            return self._b

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json))
        if url.endswith("/auth/login"):
            return _R(200, {"access_token": "tok"})
        if url.endswith("/users/vendor/create"):
            return _R(200, {"account_id": 8701})
        return _R(200, {})

    api.httpx.post = post
    api.httpx.get = lambda url, **k: _R(200, [])
    with get_session() as s:
        reply = approvals.handle_reply(PRATEEK, f"VRBTN APPROVE {old_id}", s)
    print("     ->", (reply or ["-"])[0][:170])
    create = next((b for u, b in calls if u.endswith("/users/vendor/create")), None)
    with get_session() as s:
        done = s.get(m.VendorRegistrationRequest, old_id)
        check("approved -> CREATED with Portal ref DP-8701", done.status == m.REG_CREATED and done.dealer_portal_ref == "DP-8701")
    check("Portal vendor/create got also_add_as_customer=true (the Haan from the dashboard)",
          create is not None and create["also_add_as_customer"] is True and create["gst_no"] == "07AGEPG4675R1ZR")
    row = web.get(f"/api/vendor-registrations/{old_id}").json()
    check("tab now shows APPROVED + Portal CREATED", row["stage"] == "APPROVED" and row["portal"]["state"] == "CREATED")
    api.settings.username = api.settings.password = ""

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
