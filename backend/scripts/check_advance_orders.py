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
    # Sections [1]-[6] check the ORIGINAL one-vendor-at-a-time flow, which is
    # exactly what fan-out 1 is. Quote comparison (fan-out 3) is [7] onwards.
    cfg.quote_fanout = 1
    cfg.callback_url = ""

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

    _check_quote_comparison(check, service, m, cfg, get_session, Vendor, TestClient, FastAPI, routes)

    print("\n" + ("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED"))
    return 0 if failures == 0 else 1


def _check_quote_comparison(check, service, m, cfg, get_session, Vendor, TestClient, FastAPI, routes) -> None:
    """[7] onwards: quote comparison (29 Sep 2026). Same throwaway database,
    same fake sends; the callback goes to a server on 127.0.0.1 only."""
    import hashlib
    import hmac
    import json
    import threading
    import time
    from decimal import Decimal
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from backend.app.advance_orders import callback, ranking
    from backend.app.advance_orders.parser import parse_vendor_reply
    from backend.app.integrations.whatsapp import registry

    sent: list[tuple[str, str]] = []
    service._send_text = lambda to, body: sent.append((to, body))
    service._send_template = lambda to, name, lang, params: sent.append((to, f"[template {name}] " + " | ".join(params)))
    to = lambda number: [body for t, body in sent if t == number]  # noqa: E731
    today = date(2026, 9, 17)
    ONE = [(1, "16510M68K10", 10)]
    TWO = [(1, "TT-100", 5), (2, "TT-200", 2)]

    print("\n[7] reading money in a reply")
    r = parse_vendor_reply("rate 450, 3 din", ONE, today, want_rate=True)
    check('"rate 450, 3 din" -> rate 450 in 3 days', bool(r) and r[1].quoted_rate == Decimal("450") and r[1].tat_days == 3)
    r = parse_vendor_reply("mrp 600 rate 450 2 din", ONE, today, want_rate=True)
    check('"mrp 600 rate 450" -> the MRP is never read as the rate', bool(r) and r[1].mrp == Decimal("600") and r[1].quoted_rate == Decimal("450"))
    r = parse_vendor_reply("Rs.450 each, kal tak", ONE, today, want_rate=True)
    check('"Rs.450 each, kal tak" -> 450, tomorrow', bool(r) and r[1].quoted_rate == Decimal("450") and r[1].tat_days == 1)
    r = parse_vendor_reply("haan 3 din 450", ONE, today, want_rate=True)
    check("an unlabelled number when a rate was asked is AMBIGUOUS, not a price", bool(r) and r[1].ambiguous and r[1].quoted_rate is None)
    r = parse_vendor_reply("haan 3 din", ONE, today, want_rate=False)
    check("no rate asked: a bare day count is fine", bool(r) and not r[1].ambiguous and r[1].tat_days == 3)
    check('"kuch samajh nahi aaya" is a question back, not a refusal', parse_vendor_reply("kuch samajh nahi aaya", TWO, today) is None)
    r = parse_vendor_reply("20 sep tak", ONE, date(2026, 9, 29))
    check("a date already past: available, but no ETA is promised", bool(r) and r[1].available and r[1].eta is None)
    r = parse_vendor_reply("TT-100 rate 450, TT-200 nahi, 3 din", TWO, today, want_rate=True)
    check("split answer: rate and days join up on the part they belong to", bool(r) and r[1].quoted_rate == Decimal("450") and r[1].tat_days == 3)
    check("...and a named 'nahi' is not overridden", bool(r) and not r[2].available)
    r = parse_vendor_reply("rate 450, 3 din", TWO, today, want_rate=True)
    check("one loose rate for two parts is ambiguous for both", bool(r) and r[1].ambiguous and r[2].ambiguous)
    r = parse_vendor_reply("TT-100 rate 450, TT-200 rate 300, 5 din", TWO, today, want_rate=True)
    check("a rate beside each part is read per part", bool(r) and r[1].quoted_rate == Decimal("450") and r[2].quoted_rate == Decimal("300"))

    print("\n[8] the Founder's rule: better TAT beats better discount, across a band")

    class _Q:
        def __init__(self, i):
            self.id, self.available = i, True

    def sq(i, tat, disc, prio):
        return ranking.ScoredQuote(quote=_Q(i), band=ranking.tat_band(tat), discount_pct=disc, net_price=None, vendor_priority=prio)

    check("2 days at 12% beats 10 days at 38%", ranking.best([sq(1, 2, Decimal(12), 1), sq(2, 10, Decimal(38), 2)]).quote.id == 1)
    check("inside one band, 21% beats 12%", ranking.best([sq(3, 2, Decimal(12), 1), sq(4, 3, Decimal(21), 2)]).quote.id == 4)
    check("no date given ranks behind any dated promise", ranking.best([sq(5, None, Decimal(40), 1), sq(6, 14, Decimal(5), 2)]).quote.id == 6)
    check("bands follow ADVANCE_ORDER_TAT_BANDS (1,3,7,15)", [ranking.tat_band(d) for d in (1, 2, 3, 4, 7, 8, 15, 16)] == [0, 1, 1, 2, 2, 3, 3, 4])

    print("\n[9] three vendors asked at once, answers compared")
    cfg.quote_fanout = 3
    cfg.quote_window_minutes = 60
    with get_session() as s:
        p, q, rx = Vendor(name="Pee Traders", vendor_code="PE_CT"), Vendor(name="Queue Motors", vendor_code="QU_CT"), Vendor(name="Rex Auto", vendor_code="RE_CT")
        s.add_all([p, q, rx])
        s.flush()
        s.add_all(
            [
                m.VendorBrand(brand="TATA", vendor_id=p.id, priority=1, discount_type=m.DISC_PERCENT, discount_pct=Decimal("12")),
                m.VendorBrand(brand="TATA", vendor_id=q.id, priority=2, discount_type=m.DISC_PERCENT, discount_pct=Decimal("21")),
                m.VendorBrand(brand="TATA", vendor_id=rx.id, priority=3, discount_type=m.DISC_RATE),
                # Enquiry contacts -- deliberately NOT the WhatsApp registry.
                m.AdvanceVendorContact(vendor_id=p.id, whatsapp_number="919100000001"),
                m.AdvanceVendorContact(vendor_id=q.id, whatsapp_number="919100000002"),
                m.AdvanceVendorContact(vendor_id=rx.id, whatsapp_number="919100000003"),
            ]
        )
        vid = {"p": p.id, "q": q.id, "r": rx.id}
    t1 = datetime(2026, 9, 17, 11, 0)
    service.now_ist_naive = lambda: t1
    sent.clear()
    with get_session() as s:
        order = service.create_order(
            {"external_ref": "ADV-Q1", "lines": [{"part_number": "TT-100", "brand": "TATA", "qty": 5}, {"part_number": "TT-200", "brand": "TATA", "qty": 2}]},
            s,
        )
        qid = order.id
        lid = {line.part_number: line.id for line in order.lines}
    check("all three TATA vendors asked at once", all(to(n) for n in ("919100000001", "919100000002", "919100000003")))
    check("percent vendor: one message, both parts, no rate asked", len(to("919100000001")) == 1 and "rate" not in to("919100000001")[0].lower() and "TT-100 x5" in to("919100000001")[0] and "TT-200 x2" in to("919100000001")[0])
    check("rate vendor: one message, asked for a rate beside each part", len(to("919100000003")) == 1 and "rate" in to("919100000003")[0].lower() and "TT-100 rate 450" in to("919100000003")[0])
    with get_session() as s:
        check("enquiry contacts are not in the registry (no 09:30 stock request)", not any(c.whatsapp_number.startswith("9191") for c in registry.registered_vendor_contacts(s)))

    with get_session() as s:
        service.handle_vendor_text("919100000001", "haan 2 din", s, t1 + timedelta(minutes=5))
    with get_session() as s:
        o = s.get(m.AdvanceOrder, qid)
        check("one answer in, two vendors still out: the lines wait", all(line.status == m.LINE_ASKING for line in o.lines))
    with get_session() as s:
        service.handle_vendor_text("919100000002", "haan 3 din", s, t1 + timedelta(minutes=8))
        service.handle_vendor_text("919100000003", "TT-100 rate 380, TT-200 rate 90, 10 din", s, t1 + timedelta(minutes=9))
    with get_session() as s:
        o = s.get(m.AdvanceOrder, qid)
        line = next(x for x in o.lines if x.id == lid["TT-100"])
        check("all answered: decided without waiting for the window", line.status == m.LINE_AVAILABLE)
        check("winner is Queue: same 2-3 day band as Pee, 21% beats 12%; Rex is slower", line.vendor_id == vid["q"] and line.discount_pct == Decimal("21"))
        check("...and the line says why", "21% off" in (line.note or "") and "best of 3" in (line.note or ""))
        out = service.order_out(o, s)
        ol = next(x for x in out["lines"] if x["part_number"] == "TT-100")
        check("order_out: best_quote is Queue", ol["best_quote"]["vendor_name"] == "Queue Motors")
        check("order_out: all 3 quotes, best first", ol["quote_count"] == 3 and [x["vendor_name"] for x in ol["quotes"]][0] == "Queue Motors")
        rex = next(x for x in ol["quotes"] if x["vendor_name"] == "Rex Auto")
        check("order_out: Rex's quoted rate kept, source 'quoted'", rex["quoted_rate"] == "380" and rex["price_source"] == "quoted")
        check("order_out: money travels as strings, not floats", isinstance(ol["discount_pct"], str))
        check("the order is QUOTED", o.status == m.QUOTED)

    print("\n[10] the quote window")
    sent.clear()
    with get_session() as s:
        o2 = service.create_order({"external_ref": "ADV-Q2", "lines": [{"part_number": "TT-300", "brand": "TATA", "qty": 1}]}, s)
        q2 = o2.id
        service.handle_vendor_text("919100000002", "haan 5 din", s, t1 + timedelta(minutes=10))
    with get_session() as s:
        service.tick(s, t1 + timedelta(minutes=30))
        check("30 min, one of three answered: still waiting", s.get(m.AdvanceOrder, q2).lines[0].status == m.LINE_ASKING)
    with get_session() as s:
        service.tick(s, t1 + timedelta(minutes=61))
        ln = s.get(m.AdvanceOrder, q2).lines[0]
        check("window closed: decided on what arrived", ln.status == m.LINE_AVAILABLE and ln.vendor_id == vid["q"])

    print("\n[11] ambiguity and shared numbers go to the admin")
    sent.clear()
    with get_session() as s:
        o3 = service.create_order({"external_ref": "ADV-Q3", "lines": [{"part_number": "TT-400", "brand": "TATA", "qty": 1}]}, s)
        q3 = o3.id
        service.handle_vendor_text("919100000003", "haan 3 din 450", s, t1 + timedelta(minutes=5))
    with get_session() as s:
        stored = [x for x in s.query(m.AdvanceVendorQuote).filter_by(advance_order_id=q3).all() if x.vendor_id == vid["r"]]
        check("an unlabelled rate is NOT stored as a price", not stored)
    check("...the admin is shown the vendor's words", any(t == "919999900000" and "rate saaf nahi" in b and "450" in b for t, b in sent))
    with get_session() as s:
        s1, s2 = Vendor(name="Shared One", vendor_code="S1_CT"), Vendor(name="Shared Two", vendor_code="S2_CT")
        s.add_all([s1, s2])
        s.flush()
        s.add_all(
            [
                m.VendorBrand(brand="KIA", vendor_id=s1.id, priority=1, discount_type=m.DISC_PERCENT, discount_pct=Decimal("10")),
                m.VendorBrand(brand="SKODA", vendor_id=s2.id, priority=1, discount_type=m.DISC_PERCENT, discount_pct=Decimal("10")),
                m.AdvanceVendorContact(vendor_id=s1.id, whatsapp_number="919100000099"),
                m.AdvanceVendorContact(vendor_id=s2.id, whatsapp_number="919100000099"),
            ]
        )
    sent.clear()
    with get_session() as s:
        o4 = service.create_order({"external_ref": "ADV-Q4", "lines": [{"part_number": "K-1", "brand": "KIA", "qty": 1}, {"part_number": "S-1", "brand": "SKODA", "qty": 1}]}, s)
        q4 = o4.id
        claimed = service.handle_vendor_text("919100000099", "haan 2 din", s, t1 + timedelta(minutes=5))
    with get_session() as s:
        check("one number, two vendors asked: the reply is claimed, not guessed", claimed and not s.query(m.AdvanceVendorQuote).filter_by(advance_order_id=q4).all())
    check("...and the admin is told whose it might be", any(t == "919999900000" and "Shared One" in b and "Shared Two" in b for t, b in sent))

    print("\n[12] the callback to the sales bot")
    received: list[tuple[dict, dict]] = []

    class _Hook(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((dict(self.headers), json.loads(body), body))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg.callback_url = f"http://127.0.0.1:{server.server_port}/hook"
    cfg.callback_secret = "shh"
    t5 = t1 + timedelta(minutes=20)  # asked later than ADV-Q3, still open for these vendors
    service.now_ist_naive = lambda: t5
    with get_session() as s:
        o5 = service.create_order({"external_ref": "ADV-Q5", "lines": [{"part_number": "TT-500", "brand": "TATA", "qty": 1}]}, s)
        q5 = o5.id
        for number in ("919100000001", "919100000002", "919100000003"):
            service.handle_vendor_text(number, "haan 2 din" if number != "919100000003" else "TT-500 rate 100, 2 din", s, t5 + timedelta(minutes=5))
    deadline = time.time() + 5
    while not received and time.time() < deadline:
        time.sleep(0.05)
    check("the sales bot is told when the order settles", any(p["event"] == "order.settled" and p["order"]["id"] == q5 for _, p, _ in received))
    if received:
        headers, _payload, raw = received[-1]
        expected = hmac.new(b"shh", raw, hashlib.sha256).hexdigest()
        check("...signed: X-Signature is the HMAC of the exact body", headers.get("X-Signature") == expected)
        check("...and carries the same shape GET returns", "lines" in received[-1][1]["order"] and "best_quote" in received[-1][1]["order"]["lines"][0])
    server.shutdown()
    cfg.callback_url = "http://127.0.0.1:9/nobody-home"
    try:
        callback.notify(callback.EVENT_QUOTE_UPDATED, {"id": 0})
        check("a sales bot that is down never raises into the conversation", True)
    except Exception:  # noqa: BLE001
        check("a sales bot that is down never raises into the conversation", False)
    cfg.callback_url = ""

    print("\n[13] nothing that existed before is changed")
    with get_session() as s:
        out = service.order_out(s.get(m.AdvanceOrder, qid), s)
    old_keys = {"id", "part_number", "part_name", "brand", "qty", "status", "available_qty", "eta_date", "vendor_name", "note"}
    check("every line key the sales bot already reads is still there", old_keys <= set(out["lines"][0]))
    from backend.app.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(routes.desk_router)
    app.dependency_overrides[get_current_user] = lambda: object()
    client = TestClient(app)
    cfg.enabled = True
    res = client.put("/api/vendor-brands/TATA", json={"vendor_ids": [vid["r"], vid["q"], vid["p"]]})
    tata = {row["vendor_id"]: row for row in res.json()["TATA"]}
    check("reordering a brand keeps each vendor's terms", res.status_code == 200 and tata[vid["q"]]["discount_pct"] == "21" and tata[vid["r"]]["discount_type"] == "rate")
    check("...and applies the new order", tata[vid["r"]]["priority"] == 1 and tata[vid["p"]]["priority"] == 3)
    got = client.get("/api/advance-orders/settings/quotes")
    check("the desk can read the live comparison settings", got.status_code == 200 and got.json()["quote_fanout"] == 3)
    cfg.enabled = False
    cfg.quote_fanout = 1


if __name__ == "__main__":
    sys.exit(main())
