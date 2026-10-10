"""Check of the vendor follow-up / Prateek-sir flow (Founder, 10 Oct 2026):
2-hour buffer, 2 follow-ups 15 min apart, silent vendor -> Prateek sir, who
names another vendor (asked now, saved for the brand) or says cancel.

Throwaway SQLite, fake sends -- never touches the real database:

    .venv\\Scripts\\python.exe -m backend.scripts.check_vendor_followups
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vendor-followups-check-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["ADVANCE_ORDERS_ENABLED"] = "false"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_ADMIN_PHONE_NUMBER"] = ""
os.environ["ADVANCE_ORDER_ESCALATION_NUMBERS"] = "919999492550"

failures = 0
PRATEEK = "919999492550"


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    from sqlalchemy import select

    from backend.app.advance_orders import escalation, models as m, service
    from backend.app.advance_orders.config import advance_order_settings as cfg
    from backend.app.integrations.whatsapp import outbound
    from backend.app.integrations.whatsapp import models as _wa_models  # noqa: F401 -- tables
    from core.db import get_session, init_db
    from core.models import Vendor

    init_db(force=True)
    sent: list[tuple[str, str]] = []
    service._send_text = lambda to, body: sent.append((to, body)) or f"wamid.{len(sent)}"
    service._send_template = lambda to, name, lang, params: sent.append((to, f"[template {name}]")) or f"wamid.{len(sent)}"
    service._internal_numbers = lambda session: []
    outbound.in_service_window = lambda to: True
    cfg.vendor_hours = "09:00-21:00"
    cfg.vendor_wait_minutes = 120
    cfg.followup_minutes = 15
    cfg.followup_count = 2
    cfg.vendor_template = ""
    cfg.quote_fanout = 1
    cfg.callback_url = ""
    cfg.advance_deadline_hours = 9

    with get_session() as s:
        a = Vendor(name="Alpha Motors", vendor_code="CT_AM")
        b = Vendor(name="Beta Spares", vendor_code="CT_BS")
        s.add_all([a, b])
        s.flush()
        s.add_all([
            m.VendorBrand(brand="HYUNDAI", vendor_id=a.id, priority=1),
            m.VendorBrand(brand="HYUNDAI", vendor_id=b.id, priority=2),
            m.AdvanceVendorContact(vendor_id=a.id, whatsapp_number="919100000001"),
            m.AdvanceVendorContact(vendor_id=b.id, whatsapp_number="919100000002"),
        ])
        alpha_id, beta_id = a.id, b.id

    t0 = datetime(2026, 10, 12, 10, 0)
    with get_session() as s:
        order = m.AdvanceOrder(kind=m.KIND_ADVANCE, customer_name="Test Cust")
        order.lines = [m.AdvanceOrderLine(part_number="86511C9000", brand="HYUNDAI", qty=2)]
        s.add(order)
        s.flush()
        service._advance(order, s, t0)
        oid = order.id

    print("\n[1] first vendor asked, then 2 follow-ups 15 min apart")
    check("Alpha asked first", any(to == "919100000001" for to, _ in sent))
    sent.clear()
    with get_session() as s:
        service.tick(s, t0 + timedelta(minutes=10))
    check("no follow-up at 10 min", not sent)
    with get_session() as s:
        service.tick(s, t0 + timedelta(minutes=16))
    check("follow-up 1 at 15 min", len(sent) == 1 and "Reminder 1/2" in sent[0][1])
    sent.clear()
    with get_session() as s:
        service.tick(s, t0 + timedelta(minutes=20))
    check("nothing more at 20 min", not sent)
    with get_session() as s:
        service.tick(s, t0 + timedelta(minutes=31))
    check("follow-up 2 at 30 min", len(sent) == 1 and "Reminder 2/2" in sent[0][1])
    sent.clear()
    with get_session() as s:
        service.tick(s, t0 + timedelta(minutes=90))
    check("no 3rd follow-up", not sent)

    print("\n[2] silent for 2 hours -> Prateek sir, NOT the next vendor")
    with get_session() as s:
        service.tick(s, t0 + timedelta(minutes=121))
        line = s.get(m.AdvanceOrder, oid).lines[0]
        check("line escalated", line.status == m.LINE_ESCALATED)
    check("Beta not asked", not any(to == "919100000002" for to, _ in sent))
    to_p = [body for to, body in sent if to == PRATEEK]
    check("Prateek sir told the vendor did not reply", bool(to_p) and "Alpha Motors" in to_p[-1] and "reply nahi" in to_p[-1])
    check("...with the vendor / cancel options", bool(to_p) and "vendor" in to_p[-1] and "cancel" in to_p[-1])
    sent.clear()

    print("\n[3] Prateek sir names a new vendor with his number")
    escalation.now_ist_naive = lambda: t0 + timedelta(minutes=125)
    service.now_ist_naive = lambda: t0 + timedelta(minutes=125)
    with get_session() as s:
        handled = escalation.handle_reply(PRATEEK, f"AO-{oid} vendor Gupta Auto Parts contact no 98110 22334", s)
    check("reply handled", handled)
    with get_session() as s:
        gupta = s.execute(select(Vendor).where(Vendor.name == "Gupta Auto Parts")).scalar_one_or_none()
        check("vendor created with a clean name", gupta is not None)
        gid = gupta.id if gupta else -1
        vb = s.execute(select(m.VendorBrand).where(m.VendorBrand.vendor_id == gid)).scalar_one_or_none()
        check("saved on the HYUNDAI vendor list for next time", vb is not None and vb.brand == "HYUNDAI" and vb.priority == 3)
        contact = s.execute(select(m.AdvanceVendorContact).where(m.AdvanceVendorContact.vendor_id == gid)).scalar_one_or_none()
        check("number kept as an enquiry contact", contact is not None and contact.whatsapp_number == "919811022334")
        check("line back to asking", s.get(m.AdvanceOrder, oid).lines[0].status == m.LINE_ASKING)
    check("new vendor asked", any(to == "919811022334" for to, _ in sent))
    check("Prateek sir got a confirmation", any(to == PRATEEK and "save" in body for to, body in sent))
    with get_session() as s:
        check("next time Gupta is on the HYUNDAI list", gid in [v.id for v in service.vendors_for_brand("HYUNDAI", s)])
    sent.clear()

    print("\n[4] new vendor also silent -> Prateek sir again; he cancels")
    t1 = t0 + timedelta(minutes=125)
    with get_session() as s:
        service.tick(s, t1 + timedelta(minutes=16))
        service.tick(s, t1 + timedelta(minutes=31))
    check("new vendor got 2 follow-ups", sum(1 for to, b in sent if to == "919811022334" and "Reminder" in b) == 2)
    sent.clear()
    with get_session() as s:
        service.tick(s, t1 + timedelta(minutes=121))
        check("escalated again", s.get(m.AdvanceOrder, oid).lines[0].status == m.LINE_ESCALATED)
    check("Prateek sir told about Gupta", any(to == PRATEEK and "Gupta" in b for to, b in sent))
    with get_session() as s:
        escalation.handle_reply(PRATEEK, f"AO-{oid} cancel kar do", s)
        order = s.get(m.AdvanceOrder, oid)
        check("cancelled -> order settled for the sales bot", order.lines[0].status == m.LINE_UNAVAILABLE and order.status == m.NO_VENDOR)

    print("\n[5] a vendor who says NO still passes to the next vendor")
    sent.clear()
    with get_session() as s:
        order = m.AdvanceOrder(kind=m.KIND_ADVANCE, customer_name="Cust 2")
        order.lines = [m.AdvanceOrderLine(part_number="86512C9000", brand="HYUNDAI", qty=1)]
        s.add(order)
        s.flush()
        service._advance(order, s, t0)
        oid2 = order.id
    with get_session() as s:
        service.handle_vendor_text("919100000001", "nahi hai", s, t0 + timedelta(minutes=5))
    check("Beta asked after Alpha's no", any(to == "919100000002" for to, _ in sent))
    with get_session() as s:
        check("not escalated", s.get(m.AdvanceOrder, oid2).lines[0].status == m.LINE_ASKING)

    print("\n[6] vendor: 'Please send these inquiries on this number' -> ask, then use it")
    sent.clear()
    with get_session() as s:
        order = m.AdvanceOrder(kind=m.KIND_ADVANCE, customer_name="Cust 3")
        order.lines = [m.AdvanceOrderLine(part_number="2521203000", brand="HYUNDAI", qty=5)]
        s.add(order)
        s.flush()
        service._advance(order, s, t0)
        oid3 = order.id
    sent.clear()
    with get_session() as s:
        handled = service.handle_vendor_text("919100000001", "Please send these inquiries on this number", s, t0 + timedelta(minutes=5))
    check("handled as a redirect", handled)
    check("vendor asked for the number", any(to == "919100000001" and "number" in b for to, b in sent))
    check("no 'samajh nahi aaya' to the team", not any("samajh nahi" in b for _, b in sent))
    sent.clear()
    with get_session() as s:
        service.handle_vendor_text("919100000001", "98290 11122", s, t0 + timedelta(minutes=8))
        q = s.execute(select(m.AdvanceVendorQuery).where(m.AdvanceVendorQuery.advance_order_id == oid3)).scalar_one()
        check("new number first on the question", q.numbers[0] == "919829011122")
        check("fresh buffer for the new number", q.followups_sent == 0 and q.sent_at == t0 + timedelta(minutes=8))
        c = s.execute(select(m.AdvanceVendorContact).where(m.AdvanceVendorContact.whatsapp_number == "919829011122")).scalar_one_or_none()
        check("new number saved as the vendor's contact", c is not None and c.vendor_id == alpha_id)
    check("enquiry sent to the new number", any(to == "919829011122" for to, _ in sent))
    check("Prateek sir told", any(to == PRATEEK and "919829011122" in b for to, b in sent))
    check("vendor thanked", any(to == "919100000001" and "Dhanyavaad" in b for to, b in sent))
    sent.clear()
    with get_session() as s:
        service.handle_vendor_text("919829011122", "haan 2 din", s, t0 + timedelta(minutes=20))
        check("answer from the new number is read", s.get(m.AdvanceOrder, oid3).lines[0].status == m.LINE_AVAILABLE)

    print("\n[7] a shared contact card is read as text")
    from backend.app.integrations.whatsapp import parser as wa_parser
    wa_parser.is_for_this_number = lambda value: True
    payload = {"entry": [{"changes": [{"value": {
        "messages": [{"from": "919100000001", "id": "x", "type": "contacts",
                      "contacts": [{"name": {"formatted_name": "Ramesh"}, "phones": [{"wa_id": "919829033344"}]}]}]}}]}]}
    texts = wa_parser.parse_text_messages(payload)
    check("contact card -> '[contact] Ramesh 919829033344'", bool(texts) and texts[0].text == "[contact] Ramesh 919829033344")

    print("\n[8] dashboard: add a NEW vendor with a phone at #1 -> the bot asks him first")
    sent.clear()
    with get_session() as s:
        v = service.add_vendor_to_brand("HYUNDAI", "Delta Hyundai Parts", "98111 44455", s, source="dashboard", position=1)
        did = v.id
        order = [r.vendor_id for r in s.execute(select(m.VendorBrand).where(m.VendorBrand.brand == "HYUNDAI").order_by(m.VendorBrand.priority)).scalars()]
        check("Delta is #1 on HYUNDAI, the rest moved down", order[0] == did and len(order) == len(set(order)))
        reg = s.execute(select(_wa_models.WhatsAppRegisteredNumber).where(_wa_models.WhatsAppRegisteredNumber.whatsapp_number == "919811144455")).first()
        check("not put in the stock registry", reg is None)
    with get_session() as s:
        o = m.AdvanceOrder(kind=m.KIND_ADVANCE, customer_name="Cust 4")
        o.lines = [m.AdvanceOrderLine(part_number="86513C9000", brand="HYUNDAI", qty=1)]
        s.add(o)
        s.flush()
        service._advance(o, s, t0)
    check("next HYUNDAI order asks Delta first", bool(sent) and sent[0][0] == "919811144455")
    with get_session() as s:
        again = service.add_vendor_to_brand("HYUNDAI", "Delta Hyundai Parts", "9811144455", s, source="dashboard", position=3)
        check("adding him again reuses the vendor (no duplicate)", again.id == did)
        check("...and just moves him to #3", s.execute(select(m.VendorBrand.priority).where(m.VendorBrand.vendor_id == did, m.VendorBrand.brand == "HYUNDAI")).scalar() == 3)

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
