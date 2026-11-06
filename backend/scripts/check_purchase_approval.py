"""Check: purchase approval mode (28 Oct 2026).

Throwaway SQLite, never the real database; every WhatsApp message is
captured, nothing is sent:

    .venv\\Scripts\\python.exe -m backend.scripts.check_purchase_approval
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="approval-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "PURCHASE_APPROVAL_MODE": "true",
    "ADVANCE_ORDERS_ENABLED": "true",
    "ADVANCE_ORDER_API_KEY": "k",
    "WHATSAPP_ACCESS_TOKEN": "meta-token",
    "WHATSAPP_PHONE_NUMBER_ID": "1",
})
for name in ("PURCHASE_APPROVER_NUMBERS", "PURCHASE_FORWARD_NUMBERS"):
    os.environ.pop(name, None)  # the built-in defaults are what is checked
UJJWAL, RAJKUMAR, ALAM = "918178412512", "919217396458", "919217030408"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from sqlalchemy import select
    from core.db import get_session, init_db
    from backend.app.integrations.whatsapp import client as client_mod
    from backend.app.advance_orders import approval, service
    from backend.app.advance_orders import models as m
    from backend.app.integrations.whatsapp.parser import IncomingWhatsAppText
    from backend.app.workers import document_worker

    sent: list[tuple[str, str]] = []
    approval.send_reply_safe = lambda to, body: sent.append((to, body)) or True
    vendor_loop: list[int] = []
    service._advance = lambda order, session, now: vendor_loop.append(order.id)
    # Any real WhatsApp call is a test failure.
    def _no_whatsapp(*a, **k):
        raise AssertionError("WhatsApp must not be called")

    for name in ("send_text_message", "send_template_message", "send_document"):
        if hasattr(client_mod.WhatsAppClient, name):
            setattr(client_mod.WhatsAppClient, name, _no_whatsapp)
    init_db(force=True)

    def to(number):
        return [body for n, body in sent if n == number]

    def say(number, text):
        document_worker._handle_incoming_whatsapp_text(IncomingWhatsAppText(number, f"wamid.{len(sent)}{text}", text))

    def order(ref):
        with get_session() as s:
            return s.execute(select(m.AdvanceOrder).where(m.AdvanceOrder.external_ref == ref)).scalar_one()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api.routes import advance_orders as route

    app = FastAPI()
    app.include_router(route.api_router)
    app.include_router(route.dealer_stock_router)
    tc = TestClient(app)
    body = {
        "external_ref": "ADV-TEST1",
        "customer": {"name": "Sharma Motors", "phone": "9811111111"},
        "requested_by": "sales bot",
        "needed_by": "2026-10-30",
        "lines": [
            {"part_number": "48400m69r00", "part_name": "Brake pad", "brand": "MARUTI", "qty": 2},
            {"part_number": "17220M68K00", "qty": 1},
        ],
    }

    print("\n[1] a new purchase order goes to Ujjwal sir, not the vendors")
    r = tc.post("/api/advance-orders", json=body, headers={"X-Api-Key": "k"})
    check("order accepted (201)", r.status_code == 201)
    check("status is awaiting_approval", r.json().get("status") == approval.AWAITING_APPROVAL)
    check("vendor loop never started", not vendor_loop)
    check("Ujjwal sir got one message", len(to(UJJWAL)) == 1)
    msg = (to(UJJWAL) or [""])[0]
    check("it greets him by name", msg.startswith("Namaste Ujjwal sir"))
    check("it lists the parts and qty", "48400M69R00 — Brake pad (MARUTI SUZUKI) x2" in msg and "17220M68K00 (MARUTI SUZUKI) x1" in msg)
    check("it shows customer and date", "Sharma Motors" in msg and "30 Oct 2026" in msg)
    check("it says how to answer", "APPROVE ADV-TEST1" in msg and "REJECT ADV-TEST1" in msg)
    check("nobody else was messaged", {n for n, _ in sent} == {UJJWAL})
    r = tc.post("/api/advance-orders", json=body, headers={"X-Api-Key": "k"})
    check("a repeated POST does not ask him twice", len(to(UJJWAL)) == 1)

    print("\n[2] no vendor reminders while waiting")
    with get_session() as s:
        service.tick(s)
    check("tick does nothing in approval mode", not vendor_loop)

    print("\n[3] other people cannot approve")
    sent.clear()
    with get_session() as s:
        check("not claimed for Rajkumar", not approval.handle_reply(RAJKUMAR, "APPROVE ADV-TEST1", s))
    check("Rajkumar's APPROVE is ignored", order("ADV-TEST1").status == approval.AWAITING_APPROVAL)

    print("\n[4] Ujjwal sir approves -> Rajkumar and Alam sir")
    sent.clear()
    say(UJJWAL, "approve adv-test1")
    check("order is approved", order("ADV-TEST1").status == approval.APPROVED)
    check("Rajkumar got the order", any("48400M69R00" in b and "Approved by Ujjwal sir" in b for b in to(RAJKUMAR)))
    check("Alam sir got the order", any(b.startswith("Namaste Alam sir") for b in to(ALAM)))
    check("Ujjwal sir got a confirmation", any("ADV-TEST1 approved. Sent to Rajkumar and Alam sir" in b for b in to(UJJWAL)))
    check("no vendor was messaged", {n for n, _ in sent} == {UJJWAL, RAJKUMAR, ALAM})
    sent.clear()
    say(UJJWAL, "REJECT ADV-TEST1 changed my mind")
    check("a second answer changes nothing", order("ADV-TEST1").status == approval.APPROVED and "already approved" in to(UJJWAL)[0])

    print("\n[5] reject, and short answers when one order is open")
    body2 = {**body, "external_ref": "ADV-TEST2"}
    tc.post("/api/advance-orders", json=body2, headers={"X-Api-Key": "k"})
    sent.clear()
    say(UJJWAL, "REJECT price too high")
    check("'REJECT reason' picks the only open order", order("ADV-TEST2").status == approval.REJECTED)
    check("reason is echoed back", "price too high" in (to(UJJWAL) or [""])[0])
    check("Rajkumar and Alam sir not told", not to(RAJKUMAR) and not to(ALAM))

    tc.post("/api/advance-orders", json={**body, "external_ref": "ADV-TEST3"}, headers={"X-Api-Key": "k"})
    tc.post("/api/dealer-stock-orders", json={**body, "external_ref": "ADV-TEST4"}, headers={"X-Api-Key": "k"})
    check("dealer-stock orders also wait", order("ADV-TEST4").status == approval.AWAITING_APPROVAL)
    sent.clear()
    say(UJJWAL, "APPROVE")
    check("'APPROVE' alone with two open asks which one", "Which order?" in (to(UJJWAL) or [""])[0]
          and order("ADV-TEST3").status == approval.AWAITING_APPROVAL)
    say(UJJWAL, "Approved ADV-TEST3")
    check("'Approved ADV-TEST3' works", order("ADV-TEST3").status == approval.APPROVED)

    print("\n[6] ordinary text from Ujjwal sir is not swallowed")
    with get_session() as s:
        check("'hello' is not claimed", not approval.handle_reply(UJJWAL, "hello", s))

    print("\n[7] mode off -> the old vendor flow")
    os.environ["PURCHASE_APPROVAL_MODE"] = "false"
    sent.clear()
    tc.post("/api/advance-orders", json={**body, "external_ref": "ADV-TEST5"}, headers={"X-Api-Key": "k"})
    check("order goes to the vendor loop", vendor_loop and order("ADV-TEST5").status == m.ASKING)
    check("Ujjwal sir not asked", not to(UJJWAL))
    from backend.app.integrations.whatsapp import po_output

    os.environ["PURCHASE_APPROVAL_MODE"] = "true"
    check("PO-to-vendor is off in approval mode", "approval.enabled()" in Path(po_output.__file__).read_text(encoding="utf-8"))

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
