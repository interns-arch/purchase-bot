"""Self-contained check of the advance-order flow (16 Sep 2026).

Runs against a THROWAWAY SQLite file and fake WhatsApp sends -- it never
touches the real database and never messages anyone:

    venv\\Scripts\\python.exe -m backend.scripts.check_advance_orders

Exit code 0 = every check passed."""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="advance-orders-check-"))
# Before anything imports core.db: a private database, and the feature's own
# settings. The WhatsApp side stays unconfigured, so nothing can be sent.
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["ADVANCE_ORDERS_ENABLED"] = "false"
os.environ["ADVANCE_ORDER_API_KEY"] = "check-key"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_ADMIN_PHONE_NUMBER"] = ""

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from sqlalchemy import select

    from backend.app.advance_orders import models as m
    from backend.app.advance_orders import service
    from backend.app.advance_orders.config import advance_order_settings as cfg
    from backend.app.advance_orders.parser import parse_eta, parse_vendor_reply
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
    from core.db import get_session, init_db
    from core.models import Vendor

    init_db(force=True)
    today = date(2026, 9, 16)

    print("\n[1] reading a vendor's reply")
    lines = [(1, "16510M68K10", 10), (2, "2630002752", 4)]
    r = parse_vendor_reply("haan 5 din", lines, today)
    check('"haan 5 din" -> both available, ETA +5', r is not None and all(a.available and a.eta == date(2026, 9, 21) for a in r.values()) and len(r) == 2)
    r = parse_vendor_reply("nahi hai", lines, today)
    check('"nahi hai" -> both not available', r is not None and len(r) == 2 and not any(a.available for a in r.values()))
    r = parse_vendor_reply("16510M68K10 hai 3 din\n2630002752 nahi", lines, today)
    check("per part: one in 3 days, the other not", r is not None and r[1].available and r[1].eta == date(2026, 9, 19) and not r[2].available)
    r = parse_vendor_reply("sirf 4 milenge, 20 sep tak", [(1, "16510M68K10", 10)], today)
    check('"sirf 4 milenge, 20 sep tak" -> 4 pieces by 20 Sep', r is not None and r[1].available and r[1].available_qty == 4 and r[1].eta == date(2026, 9, 20))
    check('"ready stock" is today', parse_eta("ready stock hai", today) == today)
    check('"kal" is tomorrow', parse_eta("kal bhej denge", today) == date(2026, 9, 17))
    check("a reply that says nothing about the parts is not read", parse_vendor_reply("sir call me", lines, today) is None)

    sent: list[tuple[str, str]] = []
    service._send_text = lambda to, body: sent.append((to, body))
    service._send_template = lambda to, name, lang, params: sent.append((to, f"[template {name}] " + " | ".join(params)))
    service._internal_numbers = lambda session: ["919999900000"]
    cfg.vendor_hours = "00:00-23:59"
    cfg.vendor_wait_minutes = 120
    cfg.vendor_template = ""

    with get_session() as s:
        a = Vendor(name="Alpha Motors", vendor_code="AL_CT")
        b = Vendor(name="Beta Spares", vendor_code="BE_CT")
        c = Vendor(name="Charlie Auto", vendor_code="CH_CT")
        own = Vendor(name="Company Warehouse", vendor_code="CO_CT", is_own_stock=True)
        s.add_all([a, b, c, own])
        s.flush()
        s.add_all(
            [
                WhatsAppRegisteredNumber(whatsapp_number="919000000001", vendor_id=a.id),
                WhatsAppRegisteredNumber(whatsapp_number="919000000002", vendor_id=b.id),
                WhatsAppRegisteredNumber(whatsapp_number="919000000003", vendor_id=c.id),
                WhatsAppRegisteredNumber(whatsapp_number="919000000004", vendor_id=own.id),
                m.VendorBrand(brand="MARUTI", vendor_id=own.id, priority=0),
                m.VendorBrand(brand="MARUTI", vendor_id=a.id, priority=1),
                m.VendorBrand(brand="MARUTI", vendor_id=b.id, priority=2),
                m.VendorBrand(brand="*", vendor_id=c.id, priority=1),
            ]
        )
        ids = {"a": a.id, "b": b.id, "c": c.id}

    print("\n[2] one request, vendors asked brand-wise, one by one")
    t0 = datetime(2026, 9, 16, 11, 0)
    service.now_ist_naive = lambda: t0
    payload = {
        "external_ref": "ADV-CHECK1",
        "customer": {"portal_id": 265, "name": "Kalra Motors", "phone": "919810012345"},
        "needed_by": "2026-09-25",
        "lines": [
            {"part_number": "16510M68K10", "brand": "Maruti", "qty": 10},
            {"part_number": "2630002752", "brand": "MARUTI", "qty": 4},
            {"part_number": "HONDA-1", "brand": "Honda", "qty": 1},
        ],
    }
    with get_session() as s:
        order = service.create_order(payload, s)
        oid = order.id
        by_part = {line.part_number: line.id for line in order.lines}
    to_a = [body for to, body in sent if to == "919000000001"]
    to_c = [body for to, body in sent if to == "919000000003"]
    check("the first MARUTI vendor is asked about both MARUTI parts", len(to_a) == 1 and "16510M68K10 x10" in to_a[0] and "2630002752 x4" in to_a[0])
    check("...with the needed-by date", "25 Sep tak" in to_a[0])
    check("a brand with no list goes to the '*' vendors", len(to_c) == 1 and "HONDA-1 x1" in to_c[0])
    check("the second MARUTI vendor is not asked yet", not any(to == "919000000002" for to, _ in sent))
    check("own stock is never asked", not any(to == "919000000004" for to, _ in sent))
    with get_session() as s:
        again = service.create_order(payload, s)
        check("the same external_ref twice is the same order, nobody asked again", again.id == oid and len(sent) == 2)

    print("\n[3] replies, the next vendor, silence, the answer")
    with get_session() as s:
        check("a text from a number with no open question is not claimed", not service.handle_vendor_text("919000000002", "haan", s, t0))
    with get_session() as s:
        claimed = service.handle_vendor_text("919000000003", "sir call me", s, t0 + timedelta(minutes=5))
    check("an unreadable reply is claimed and forwarded to the admins", claimed and any(to == "919999900000" and "samajh nahi aaya" in body for to, body in sent))
    with get_session() as s:
        service.handle_vendor_text("+91 90000 00001", "16510M68K10 hai 5 din, 2630002752 nahi", s, t0 + timedelta(minutes=10))
        o = s.get(m.AdvanceOrder, oid)
        l1 = next(x for x in o.lines if x.id == by_part["16510M68K10"])
        l2 = next(x for x in o.lines if x.id == by_part["2630002752"])
        check("vendor A: 16510M68K10 available in 5 days, from A", l1.status == m.LINE_AVAILABLE and l1.eta_date == date(2026, 9, 21) and l1.vendor_id == ids["a"])
        check("...2630002752 still open", l2.status == m.LINE_ASKING)
    check("...so the next MARUTI vendor is asked about 2630002752 only", any(to == "919000000002" and "2630002752 x4" in body and "16510M68K10" not in body for to, body in sent))
    with get_session() as s:
        service.handle_vendor_text("919000000003", "haan kal", s, t0 + timedelta(minutes=20))
        o = s.get(m.AdvanceOrder, oid)
        out = service.order_out(o, s)
        l3 = next(x for x in out["lines"] if x["part_number"] == "HONDA-1")
        check("vendor C: HONDA-1 tomorrow", l3["status"] == "available" and l3["eta_date"] == "2026-09-17")
        check("...and the order is still asking (vendor B has not answered)", o.status == m.ASKING)
    with get_session() as s:
        service.tick(s, t0 + timedelta(hours=3))
        o = s.get(m.AdvanceOrder, oid)
        l2 = next(x for x in o.lines if x.id == by_part["2630002752"])
        check("vendor B silent past the wait: nobody left, 2630002752 is not available", l2.status == m.LINE_UNAVAILABLE)
        check("...and with every line answered the order is QUOTED", o.status == m.QUOTED)

    print("\n[4] the customer decides")
    sent.clear()
    with get_session() as s:
        o = s.get(m.AdvanceOrder, oid)
        service.confirm_order(o, [by_part["16510M68K10"]], s)
        statuses = {x.part_number: x.status for x in o.lines}
        check("confirm with one line: it is ordered, the other available one is dropped", statuses["16510M68K10"] == m.LINE_ORDERED and statuses["HONDA-1"] == m.LINE_CANCELLED)
        check("...the order is CONFIRMED", o.status == m.CONFIRMED)
    check("vendor A gets the order", any(to == "919000000001" and "Order pakka" in body and "16510M68K10 x10" in body for to, body in sent))
    check("vendor C, whose part was dropped, gets nothing", not any(to == "919000000003" for to, _ in sent))
    check("the admins / purchase team are told", any(to == "919999900000" and "CONFIRMED" in body and "Kalra Motors" in body for to, body in sent))
    with get_session() as s:
        try:
            service.cancel_order(s.get(m.AdvanceOrder, oid), s)
            check("a confirmed order cannot be cancelled here", False)
        except ValueError:
            check("a confirmed order cannot be cancelled here", True)

    print("\n[5] vendor hours")
    sent.clear()
    cfg.vendor_hours = "09:30-19:00"
    night = datetime(2026, 9, 16, 22, 0)
    service.now_ist_naive = lambda: night
    with get_session() as s:
        o2 = service.create_order({"external_ref": "ADV-CHECK2", "lines": [{"part_number": "X1", "brand": "MARUTI", "qty": 2}]}, s)
        oid2 = o2.id
    check("at 22:00 nobody is messaged", not sent)
    with get_session() as s:
        service.tick(s, datetime(2026, 9, 17, 9, 45))
    check("at 09:45 the queued question goes out", any(to == "919000000001" and "X1 x2" in body for to, body in sent))
    with get_session() as s:
        service.cancel_order(s.get(m.AdvanceOrder, oid2), s)
        q = s.execute(select(m.AdvanceVendorQuery).where(m.AdvanceVendorQuery.advance_order_id == oid2)).scalars().all()
        check("cancel closes the open question", all(x.status == m.Q_CLOSED for x in q))
    with get_session() as s:
        check("...and a late reply to it is not claimed", not service.handle_vendor_text("919000000001", "haan", s, datetime(2026, 9, 17, 10, 0)))

    print("\n[6] the API")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.app.api.routes import advance_orders as routes

    app = FastAPI()
    app.include_router(routes.api_router)
    client = TestClient(app)
    body = {"external_ref": "ADV-API1", "lines": [{"part_number": "Y1", "brand": "MARUTI", "qty": 1}]}
    cfg.enabled = False
    check("switched off: 503", client.post("/api/advance-orders", json=body, headers={"X-Api-Key": "check-key"}).status_code == 503)
    cfg.enabled = True
    cfg.api_key = "check-key"
    check("no key: 401", client.post("/api/advance-orders", json=body).status_code == 401)
    check("wrong key: 401", client.post("/api/advance-orders", json=body, headers={"X-Api-Key": "nope"}).status_code == 401)
    res = client.post("/api/advance-orders", json=body, headers={"X-Api-Key": "check-key"})
    check("right key: 201 with an id and status asking", res.status_code == 201 and res.json()["status"] == "asking")
    new_id = res.json()["id"]
    got = client.get(f"/api/advance-orders/{new_id}", headers={"X-Api-Key": "check-key"})
    check("GET returns the lines", got.status_code == 200 and got.json()["lines"][0]["part_number"] == "Y1")
    check("confirm before any answer: 409", client.post(f"/api/advance-orders/{new_id}/confirm", json={}, headers={"X-Api-Key": "check-key"}).status_code == 409)
    check("cancel: 200 and cancelled", client.post(f"/api/advance-orders/{new_id}/cancel", headers={"X-Api-Key": "check-key"}).json()["status"] == "cancelled")
    check("an empty line list: 422", client.post("/api/advance-orders", json={"lines": []}, headers={"X-Api-Key": "check-key"}).status_code == 422)
    cfg.enabled = False

    print("\n" + ("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
