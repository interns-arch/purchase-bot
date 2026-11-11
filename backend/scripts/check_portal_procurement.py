"""Check of Portal procurement end to end (brief, 11 Nov 2026), against a FAKE
Dealer Portal (httpx.MockTransport) and fake WhatsApp sends, on a throwaway
SQLite database -- never touches the real Portal, Odoo, WhatsApp or DB:

    .venv\\Scripts\\python.exe -m backend.scripts.check_portal_procurement

Covers: success, partial split, unmapped vendor, stale quantity, approval
rejection, allocation timeout, Odoo timeout, PDF failure, WhatsApp failure,
401 handling, duplicate job execution, and the engine scope (sales-bot
orders stay off)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_TMP = Path(tempfile.mkdtemp(prefix="portal-procurement-check-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["ADVANCE_ORDERS_ENABLED"] = "false"
os.environ["PURCHASE_APPROVAL_MODE"] = "true"  # as on the server: must not hold Portal demand
os.environ["PORTAL_PROCUREMENT_ENABLED"] = "true"
os.environ["PORTAL_PROCUREMENT_USERNAME"] = "svc-test"
os.environ["PORTAL_PROCUREMENT_PASSWORD"] = "pw-test"
os.environ["PORTAL_PROCUREMENT_NOTIFY_NUMBERS"] = "919000000099"
os.environ["PORTAL_PROCUREMENT_ALERT_NUMBERS"] = "919000000098"
os.environ["PORTAL_PROCUREMENT_PDF_DIR"] = str(_TMP / "po")
os.environ["DEALER_PORTAL_CF_CLIENT_ID"] = "cf-id"
os.environ["DEALER_PORTAL_CF_CLIENT_SECRET"] = "cf-secret"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_ADMIN_PHONE_NUMBER"] = ""
os.environ["ADVANCE_ORDER_ESCALATION_NUMBERS"] = ""

import httpx  # noqa: E402

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


NOTIFY = "919000000099"
ALERT = "919000000098"


def make_po_pdf(odoo_name: str, parts: list[str]) -> bytes:
    from backend.scripts._pdf_fixture import make_pdf

    path = _TMP / f"{odoo_name}.src.pdf"
    text = [(50, 800, 14, f"Purchase Order {odoo_name}")]
    text += [(50, 760 - 20 * i, 11, f"{p}   qty") for i, p in enumerate(parts)]
    text += [(50, 400, 9, "padding " * 40)]
    make_pdf(path, [{"text": text}])
    return path.read_bytes()


class FakePortal:
    """Just enough of the Dealer Portal to drive every step."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.options: dict[int, list[dict]] = {}
        self.allocs: list[dict] = []
        self.preview: dict[str, dict] = {}
        self.approval: dict[str, str] = {}
        self.odoo: dict[str, str] = {}
        self.parts_by_po: dict[str, list[str]] = {}
        self.calls: Counter = Counter()
        self.payloads: list[dict] = []
        self.allocate_mode = "ok"
        self.push_mode = "ok"
        self.pdf_mode = "ok"
        self.unauthorized = 0  # answer this many requests with 401
        self.headers_seen: list[dict] = []
        self.next_po = 70

    def add_row(self, sol: int, part: str, qty: int, brand: str = "HYUNDAI", po_grp: str | None = None) -> None:
        self.rows[sol] = {
            "row_type": "source", "source_order_line_id": sol, "source_po_group_id": po_grp or f"PO-GRP-{sol}",
            "part_no": part, "brand": brand, "planned_quantity": qty, "procurement_stage": "pending",
            "order_date": "2026-11-10", "buyer_dealer_id": 4587,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = url.path.replace("/api/v1", "")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls[(request.method, path)] += 1
        self.headers_seen.append(dict(request.headers))
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        if path == "/auth/login":
            return httpx.Response(200, json={"access_token": "jwt-1", "expires_in": 28800})
        if self.unauthorized > 0:
            self.unauthorized -= 1
            return httpx.Response(401, json={"detail": "expired"})
        if path == "/procurement/active" and request.method == "GET":
            if q.get("include_po_mst") == "true":
                items = [{"row_type": "po_mst", "po_mst": a["po_mst"], "quote_reference": a["quote_reference"],
                          "supplier_dealer_id": a["dealer"], "planned_quantity": a["qty"]} for a in self.allocs]
                return httpx.Response(200, json={"items": items, "total": len(items)})
            items = [r for r in self.rows.values()
                     if (not q.get("part_no") or r["part_no"] == q["part_no"])
                     and (not q.get("po_group_id") or r["source_po_group_id"] == q["po_group_id"])]
            off, lim = int(q.get("offset", 0)), int(q.get("limit", 500))
            return httpx.Response(200, json={"items": items[off:off + lim], "total": len(items)})
        if path.startswith("/procurement/active/") and path.endswith("/vendors"):
            sol = int(path.split("/")[3])
            return httpx.Response(200, json={"vendors": self.options.get(sol, [])})
        if path == "/procurement/active/allocate":
            if self.allocate_mode == "409":
                return httpx.Response(409, json={"detail": "vendor not mapped to brand"})
            self.payloads.append(body)
            po = f"PO-MST-2026-{self.next_po:04d}"
            self.next_po += 1
            row = self.rows[body["source_order_line_id"]]
            row["planned_quantity"] -= body["planned_quantity"]
            if row["planned_quantity"] <= 0:
                row["procurement_stage"] = "allocated"
            self.allocs.append({"po_mst": po, "dealer": body["supplier_dealer_id"], "qty": body["planned_quantity"],
                                "quote_reference": body.get("quote_reference")})
            self.parts_by_po[po] = [row["part_no"]]
            if self.allocate_mode == "timeout":
                raise httpx.ReadTimeout("lost", request=request)
            return httpx.Response(200, json={"batch_id": f"B{po[-2:]}", "masters": [
                {"po_mst": po, "vendor_id": body["supplier_dealer_id"], "vendor": f"Dealer {body['supplier_dealer_id']}",
                 "brand": row["brand"], "quantity": body["planned_quantity"], "arrival_date": "2026-11-12"}]})
        if path == "/procurement/knockoff/preview":
            return httpx.Response(200, json=self.preview.get(body["po_group_id"], {
                "safe_matches": [{"supply_order_line_id": 15001, "quantity": 1}], "blocked": [], "future": [],
                "requires_confirmation": True}))
        if path == "/procurement/knockoff/commit":
            return httpx.Response(200, json={"ok": True})
        if path == "/procurement/knockoff/duplicate-approval":
            self.approval[body["po_group_id"]] = "pending"
            return httpx.Response(200, json={"status": "pending"})
        if path.startswith("/procurement/knockoff/duplicate-approval/"):
            po = path.rsplit("/", 1)[1]
            if po not in self.approval:
                return httpx.Response(404, json={"detail": "none"})
            return httpx.Response(200, json={"status": self.approval[po], "reason": "stock already in transit"})
        if path == "/purchase-orders/push-to-odoo":
            if self.push_mode == "409":
                return httpx.Response(409, json={"detail": "missing Odoo partner"})
            po = body["po_group_id"]
            self.odoo[po] = f"P{po[-4:]}"
            if self.push_mode == "timeout":
                raise httpx.ReadTimeout("lost", request=request)
            return httpx.Response(200, json={"odoo_po_id": 900 + int(po[-2:]), "odoo_po_name": self.odoo[po]})
        if path.startswith("/purchase-orders/"):
            po = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"po_group_id": po, "odoo_po_name": self.odoo.get(po),
                                             "odoo_po_id": None})
        if path == "/odoo/documents/po/pdf":
            ref = q["odoo_ref"]
            po = next((p for p, name in self.odoo.items() if name == ref or str(900 + int(p[-2:])) == ref), None)
            if self.pdf_mode == "html":
                return httpx.Response(200, text="<html>error</html>", headers={"content-type": "text/html"})
            name = self.odoo.get(po, ref)
            return httpx.Response(200, content=make_po_pdf(name, self.parts_by_po.get(po, [])),
                                  headers={"content-type": "application/pdf"})
        return httpx.Response(404, json={"detail": f"no route {path}"})


def main() -> int:
    from sqlalchemy import select

    from backend.app.advance_orders import models as am, service as engine
    from backend.app.advance_orders.config import advance_order_settings as cfg
    from backend.app.integrations.whatsapp import models as _wa  # noqa: F401
    from backend.app.integrations.whatsapp import outbound
    from backend.app.portal_procurement import models as m, notify, service
    from backend.app.portal_procurement.client import AuthStopped, PortalClient
    from core.db import get_session, init_db
    from core.models import DealerPortalVendorMap, DealerPortalVendorMapStatus, Vendor

    init_db(force=True)
    clock = {"now": datetime(2026, 11, 11, 10, 0)}
    now = lambda: clock["now"]  # noqa: E731
    engine.now_ist_naive = now
    service.now_ist_naive = now

    sent: list[tuple[str, str]] = []
    engine._send_text = lambda to, body: sent.append((to, body)) or f"wamid.{len(sent)}"
    engine._send_template = lambda to, name, lang, params: sent.append((to, f"[tpl {name}] {params}")) or "wamid.t"
    engine._internal_numbers = lambda session: []
    outbound.in_service_window = lambda to: True
    pdfs: list[tuple[str, str]] = []
    pdf_fail = {"on": False}

    def fake_send_pdf(number, po, caption, cache):
        if pdf_fail["on"]:
            raise RuntimeError("WhatsApp down")
        pdfs.append((number, po.po_mst))
        return f"wamid.pdf{len(pdfs)}"

    notify._send_pdf = fake_send_pdf
    cfg.vendor_hours = "00:00-23:59"
    cfg.vendor_template = ""
    cfg.quote_fanout = 50
    cfg.callback_url = ""
    cfg.followup_count = 0

    portal = FakePortal()
    client = PortalClient(transport=httpx.MockTransport(portal.handler), sleep=lambda s: None,
                          base_url="https://portal.test/api/v1")

    with get_session() as s:
        vendors = {}
        for code, name, number in (("A", "Alpha Motors", "919100000001"), ("B", "Beta Spares", "919100000002"),
                                   ("C", "Gamma Auto", "919100000003")):
            v = Vendor(name=name, vendor_code=f"CT_{code}")
            s.add(v)
            s.flush()
            vendors[code] = v.id
            s.add(am.VendorBrand(brand="HYUNDAI", vendor_id=v.id, priority=1, discount_type=am.DISC_PERCENT,
                                 discount_pct=20))
            s.add(am.AdvanceVendorContact(vendor_id=v.id, whatsapp_number=number))
        s.add(DealerPortalVendorMap(vendor_id=vendors["A"], vendor_name="Alpha Motors", dp_dealer_id=27,
                                    status=DealerPortalVendorMapStatus.CONFIRMED))
        s.add(DealerPortalVendorMap(vendor_id=vendors["B"], vendor_name="Beta Spares", dp_dealer_id=28,
                                    status=DealerPortalVendorMapStatus.CONFIRMED))
        # Gamma has NO Portal mapping.
        sales = am.AdvanceOrder(kind=am.KIND_ADVANCE, customer_name="old sales-bot order", status=am.ASKING)
        sales.lines = [am.AdvanceOrderLine(part_number="OLD123", brand="HYUNDAI", qty=1)]
        s.add(sales)
        s.flush()
        sales_id = sales.id

    def run_poll():
        with get_session() as s:
            service.poll(s, client, clock["now"].date())
            s.commit()
            service.enquire(s)

    def run_steps(times: int = 1):
        for _ in range(times):
            with get_session() as s:
                engine.tick(s, clock["now"])
            with get_session() as s:
                service.run_steps(s, client, clock["now"].date())
            clock["now"] += timedelta(minutes=30)
            with get_session() as s:  # nothing waits on a back-off timer in the check
                for row in s.execute(select(m.PortalAllocation)).scalars():
                    row.next_try_at = None
                for row in s.execute(select(m.PortalPoMaster)).scalars():
                    row.next_try_at = None

    def reply(number: str, text: str) -> bool:
        with get_session() as s:
            return engine.handle_vendor_text(number, text, s, clock["now"])

    def asked(number: str) -> int:
        return sum(1 for to, _ in sent if to == number)

    def demand(sol: int):
        with get_session() as s:
            d = s.execute(select(m.PortalDemandLine).where(m.PortalDemandLine.source_order_line_id == sol)).scalar_one()
            s.expunge(d)
            return d

    def allocs(sol: int):
        with get_session() as s:
            d = s.execute(select(m.PortalDemandLine).where(m.PortalDemandLine.source_order_line_id == sol)).scalar_one()
            rows = s.execute(select(m.PortalAllocation).where(m.PortalAllocation.demand_line_id == d.id)
                             .order_by(m.PortalAllocation.id)).scalars().all()
            for r in rows:
                s.expunge(r)
            return rows

    def po(po_mst: str):
        with get_session() as s:
            r = s.execute(select(m.PortalPoMaster).where(m.PortalPoMaster.po_mst == po_mst)).scalar_one()
            s.expunge(r)
            return r

    for sol in (12470,):
        portal.options[sol] = [{"dealer_id": 27, "name": "Alpha"}, {"dealer_id": 28, "name": "Beta"}]

    # ------------------------------------------------------------------ 1
    print("\n[1] success: quickest vendor wins, safe knockoff, Odoo, PDF to purchase user")
    portal.add_row(12470, "86511C9000", 2)
    portal.add_row(99, "IGNORED", 0)  # planned 0 -> ignored
    portal.rows[99]["planned_quantity"] = 0
    run_poll()
    check("line discovered and sent to vendors", demand(12470).state == m.ENQUIRY_SCHEDULED)
    check("all 3 brand vendors asked at once", all(asked(n) == 1 for n in ("919100000001", "919100000002",
                                                                         "919100000003")))
    check("Portal demand not held by purchase approval mode", demand(12470).advance_order_id is not None)
    check("old sales-bot order not ticked (switch off)", not any("OLD123" in body for _, body in sent))
    check("every call carried the Cloudflare headers",
          all(h.get("cf-access-client-id") == "cf-id" for h in portal.headers_seen))
    reply("919100000001", "haan 1 din")
    reply("919100000002", "haan 5 din")
    reply("919100000003", "nahi")
    run_steps(6)
    a = allocs(12470)
    check("one allocation, to Alpha (dealer 27)", len(a) == 1 and a[0].supplier_dealer_id == 27)
    p = portal.payloads[0] if portal.payloads else {}
    check("allocate payload has quote fields + vendor_bot", p.get("quote_source") == "vendor_bot"
          and p.get("quote_reference", "").startswith("PH-Q-") and p.get("quoted_tat_days") == 1
          and p.get("planned_quantity") == 2 and p.get("quoted_discount_percent") == 20.0)
    first_po = a[0].masters[0]["po_mst"] if a and a[0].masters else ""
    check("safe knockoff committed, no approval asked",
          portal.calls[("POST", "/procurement/knockoff/commit")] == 1
          and portal.calls[("POST", "/procurement/knockoff/duplicate-approval")] == 0)
    check("pushed to Odoo once", portal.calls[("POST", "/purchase-orders/push-to-odoo")] == 1)
    check("PO-MST completed", po(first_po).state == m.COMPLETED if first_po else False)
    check("PDF sent once, to the purchase user only", pdfs == [(NOTIFY, first_po)])
    check("PDF checksum stored", bool(po(first_po).pdf_sha256) if first_po else False)
    check("vendor NOT sent an order confirmation", not any("Order pakka" in b for _, b in sent))
    created = [b for to, b in sent if to == NOTIFY and "PO-MST created" in b]
    check("purchase user told the PO-MST was created, once, with its details",
          len(created) == 1 and first_po in created[0] and "86511C9000 x2" in created[0]
          and "PO-GRP-12470" in created[0])
    check("...and nobody else got that message",
          not any("PO-MST created" in b for to, b in sent if to != NOTIFY))
    check("demand completed", demand(12470).state == m.COMPLETED)

    print("\n[1b] the whole job again: nothing is duplicated")
    before = (len(sent), len(portal.payloads), portal.calls[("POST", "/purchase-orders/push-to-odoo")], len(pdfs))
    run_poll()
    run_steps(3)
    after = (len(sent), len(portal.payloads), portal.calls[("POST", "/purchase-orders/push-to-odoo")], len(pdfs))
    check("no new enquiry, allocation, Odoo push or PDF", before == after)

    # ------------------------------------------------------------------ 2
    print("\n[2] partial split across two vendors")
    portal.add_row(12471, "86512C9000", 5)
    portal.options[12471] = [{"dealer_id": 27}, {"dealer_id": 28}]
    run_poll()
    reply("919100000001", "sirf 3 milenge, 1 din")
    reply("919100000002", "haan 3 din")
    reply("919100000003", "nahi")
    run_steps(8)
    a = allocs(12471)
    check("two allocations: 3 from Alpha, 2 from Beta",
          sorted((x.supplier_dealer_id, x.quantity) for x in a) == [(27, 3), (28, 2)])
    check("both PO-MSTs completed", all(po(x.masters[0]["po_mst"]).state == m.COMPLETED for x in a if x.masters))
    check("never more than the Portal planned quantity", sum(x.quantity for x in a) == 5)

    # ------------------------------------------------------------------ 3
    print("\n[3] unmapped winner -> onboarding, never another dealer's id")
    portal.add_row(12472, "86513C9000", 1)
    portal.options[12472] = [{"dealer_id": 27}]
    run_poll()
    reply("919100000003", "haan aaj")
    reply("919100000001", "haan 7 din")
    reply("919100000002", "nahi")
    calls_before = len(portal.payloads)
    run_steps(3)
    a = allocs(12472)
    check("Gamma won and is waiting for onboarding", len(a) == 1 and a[0].state == m.VENDOR_ONBOARDING_REQUIRED)
    check("no allocation was posted", len(portal.payloads) == calls_before)
    check("purchase team alerted about onboarding", any(to == ALERT and "not set up on the Dealer Portal" in b
                                                         for to, b in sent))
    portal.options[12472].append({"dealer_id": 31, "gstin": "07ABCDE1234F1Z5"})
    with get_session() as s:
        s.add(DealerPortalVendorMap(vendor_id=vendors["C"], vendor_name="Gamma Auto", dp_dealer_id=31,
                                    status=DealerPortalVendorMapStatus.CONFIRMED))
    run_steps(6)
    a = allocs(12472)
    check("after onboarding it continues with dealer 31", a[0].supplier_dealer_id == 31
          and a[0].state == m.PO_MST_CREATED)

    # ------------------------------------------------------------------ 4
    print("\n[4] stale quantity: re-fetched right before allocation")
    portal.add_row(12473, "86514C9000", 4)
    portal.options[12473] = [{"dealer_id": 27}, {"dealer_id": 28}]
    run_poll()
    reply("919100000001", "haan 1 din")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    portal.rows[12473]["planned_quantity"] = 1  # someone planned 3 elsewhere meanwhile
    n = len(portal.payloads)
    run_steps(2)
    check("allocated the current 1, not the stale 4",
          len(portal.payloads) == n + 1 and portal.payloads[-1]["planned_quantity"] == 1)
    portal.add_row(12474, "86515C9000", 2)
    portal.options[12474] = [{"dealer_id": 27}]
    run_poll()
    reply("919100000001", "haan 2 din")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    portal.rows[12474]["planned_quantity"] = 0
    n = len(portal.payloads)
    run_steps(2)
    check("demand gone to 0 -> allocation cancelled, nothing posted",
          len(portal.payloads) == n and allocs(12474)[0].state == m.ALLOCATION_CANCELLED)

    # ------------------------------------------------------------------ 5
    print("\n[5] blocked stock -> Super Admin approval -> rejected")
    portal.add_row(12475, "86516C9000", 1)
    portal.options[12475] = [{"dealer_id": 28}]
    portal.preview["PO-MST-2026-%04d" % portal.next_po] = {
        "safe_matches": [], "blocked": [{"supply_order_line_id": 16001, "quantity": 1}], "future": [],
        "requires_confirmation": True}
    rejected_po = "PO-MST-2026-%04d" % portal.next_po
    run_poll()
    reply("919100000002", "haan 2 din")
    reply("919100000001", "nahi")
    reply("919100000003", "nahi")
    run_steps(3)
    check("approval requested once", portal.calls[("POST", "/procurement/knockoff/duplicate-approval")] == 1
          and po(rejected_po).state == m.APPROVAL_PENDING)
    run_steps(2)
    check("no second approval while pending", portal.calls[("POST", "/procurement/knockoff/duplicate-approval")] == 1)
    portal.approval[rejected_po] = "rejected"
    portal.rows[12475]["planned_quantity"] = 1  # Portal shows the demand again
    portal.rows[12475]["procurement_stage"] = "pending"
    pushes = portal.calls[("POST", "/purchase-orders/push-to-odoo")]
    run_steps(1)
    check("rejected PO-MST never pushed to Odoo", portal.calls[("POST", "/purchase-orders/push-to-odoo")] == pushes
          and po(rejected_po).state == m.APPROVAL_REJECTED)
    check("admin reason kept", po(rejected_po).approval_reason == "stock already in transit")
    check("allocation marked rejected/cancelled", allocs(12475)[0].state == m.ALLOCATION_CANCELLED)
    check("source line reopened for vendor selection", demand(12475).state == m.DISCOVERED)
    check("purchase team told about the rejection", any("REJECTED" in b for to, b in sent if to == ALERT))

    # ------------------------------------------------------------------ 6
    print("\n[6] allocation POST times out after the Portal acted -> reconciled, not re-posted")
    portal.add_row(12476, "86517C9000", 1)
    portal.options[12476] = [{"dealer_id": 27}]
    run_poll()
    reply("919100000001", "haan 1 din")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    portal.allocate_mode = "timeout"
    n = len(portal.payloads)
    run_steps(1)
    check("state = allocation_unknown after the timeout", allocs(12476)[0].state == m.ALLOCATION_UNKNOWN)
    portal.allocate_mode = "ok"
    run_steps(1)
    check("reconciled to po_mst_created with ONE allocate call",
          allocs(12476)[0].state in (m.PO_MST_CREATED,) and len(portal.payloads) == n + 1)

    # ------------------------------------------------------------------ 7
    print("\n[7] Odoo push times out after Odoo created the PO -> reconciled by GET")
    portal.add_row(12477, "86518C9000", 1)
    portal.options[12477] = [{"dealer_id": 27}]
    run_poll()
    reply("919100000001", "haan 1 din")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    portal.push_mode = "timeout"
    pushes = portal.calls[("POST", "/purchase-orders/push-to-odoo")]
    run_steps(3)
    portal.push_mode = "ok"
    run_steps(4)
    a = allocs(12477)
    the_po = a[0].masters[0]["po_mst"] if a and a[0].masters else ""
    check("Odoo push sent exactly once", portal.calls[("POST", "/purchase-orders/push-to-odoo")] == pushes + 1)
    check("PO-MST reached completed via reconcile", bool(the_po) and po(the_po).state == m.COMPLETED)

    # ------------------------------------------------------------------ 8
    print("\n[8] PDF failure (not a PDF) -> not sent; then WhatsApp failure -> retried, sent once")
    portal.add_row(12478, "86519C9000", 1)
    portal.options[12478] = [{"dealer_id": 27}]
    run_poll()
    reply("919100000001", "haan 1 din")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    portal.pdf_mode = "html"
    sent_pdfs = len(pdfs)
    run_steps(4)
    a = allocs(12478)
    the_po = a[0].masters[0]["po_mst"] if a and a[0].masters else ""
    check("bad PDF never sent; PO stays at odoo_posted", bool(the_po) and po(the_po).state == m.ODOO_POSTED
          and len(pdfs) == sent_pdfs)
    portal.pdf_mode = "ok"
    pdf_fail["on"] = True
    with get_session() as s:
        row = s.execute(select(m.PortalPoMaster).where(m.PortalPoMaster.po_mst == the_po)).scalar_one()
        row.attempts = 0
    run_steps(2)
    check("WhatsApp failure: not marked notified", po(the_po).state == m.PDF_DOWNLOADED and len(pdfs) == sent_pdfs)
    pdf_fail["on"] = False
    run_steps(2)
    check("after WhatsApp recovers: PDF sent exactly once, PO not recreated",
          pdfs.count((NOTIFY, the_po)) == 1 and po(the_po).state == m.COMPLETED)

    # ------------------------------------------------------------------ 9
    print("\n[9] 401: refreshed once; a second 401 stops the run")
    portal.unauthorized = 1
    try:
        client.get("/procurement/active", {"date_from": "2026-11-05", "date_to": "2026-11-11"})
        check("one 401 -> re-login -> success", True)
    except Exception:  # noqa: BLE001
        check("one 401 -> re-login -> success", False)
    portal.unauthorized = 5
    logins = portal.calls[("POST", "/auth/login")]
    try:
        client.get("/procurement/active", {})
        check("two 401s -> AuthStopped", False)
    except AuthStopped:
        check("two 401s -> AuthStopped", True)
    check("no login loop (one extra login only)", portal.calls[("POST", "/auth/login")] == logins + 1)
    portal.unauthorized = 0

    # ------------------------------------------------------------------ 10
    print("\n[10] scope + secrets")
    with get_session() as s:
        check("old sales-bot order untouched", s.get(am.AdvanceOrder, sales_id).status == am.ASKING
              and not s.execute(select(am.AdvanceVendorQuery).where(
                  am.AdvanceVendorQuery.advance_order_id == sales_id)).first())
        audit = " ".join(f"{t.error or ''}" for t in s.execute(select(m.PortalTransition)).scalars())
    check("no secret in the audit log", not any(x in audit for x in ("pw-test", "jwt-1", "cf-secret")))
    check("no secret in any WhatsApp text", not any(x in b for _, b in sent for x in ("pw-test", "jwt-1", "cf-secret")))
    with get_session() as s:
        moves = s.execute(select(m.PortalTransition)).scalars().all()
    check("every transition recorded", len(moves) > 40)

    # ------------------------------------------------------------------ 11
    print("\n[11] vendor with a WhatsApp group is asked IN the group, never one to one")
    from backend.app.integrations.whatsapp import whapi

    group_posts: list[tuple[str, str]] = []
    whapi.enabled = lambda: True
    whapi.vendor_groups = lambda: {"CT_A": "120363000000000001@g.us"}
    whapi.send_text = lambda chat, body: group_posts.append((chat, body)) or f"whapi.{len(group_posts)}"
    cfg.group_gap_seconds = 0
    cfg.followup_count = 1
    cfg.followup_minutes = 15
    portal.add_row(12479, "86520C9000", 1)
    portal.options[12479] = [{"dealer_id": 27}]
    before_alpha = asked("919100000001")
    run_poll()
    check("Alpha's question posted in his group", any(c == "120363000000000001@g.us" and "86520C9000" in b
                                                      for c, b in group_posts))
    check("Alpha NOT messaged one to one", asked("919100000001") == before_alpha)
    check("Beta (no group) still asked on his number", any(to == "919100000002" and "86520C9000" in b
                                                           for to, b in sent))
    check("nothing about the Portal said in the group", not any("portal" in b.lower() for _, b in group_posts))
    clock["now"] += timedelta(minutes=16)
    with get_session() as s:
        engine.tick(s, clock["now"])
    check("reminder also goes to the group", sum(1 for c, b in group_posts if "Reminder" in b) == 1
          and asked("919100000001") == before_alpha)
    # A reply in the group reaches the bot as the vendor's main number.
    check("group reply is read", reply("919100000001", "haan 1 din"))
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    run_steps(6)
    a = allocs(12479)
    check("...and the order goes ahead to dealer 27", len(a) == 1 and a[0].supplier_dealer_id == 27
          and a[0].state == m.PO_MST_CREATED)
    cfg.vendor_route = "group_only"
    portal.add_row(12480, "86521C9000", 1)
    portal.options[12480] = [{"dealer_id": 27}]
    before_beta = asked("919100000002")
    run_poll()
    check("group_only: vendor without a group is not asked", asked("919100000002") == before_beta)
    cfg.vendor_route = "group_first"

    # ------------------------------------------------------------------ 12
    print("\n[12] prices: MRP includes GST, discount on MRP, ex-GST = net / 1.18")
    from decimal import Decimal
    from types import SimpleNamespace

    class _S:
        def __init__(self, quote):
            self.quote = quote

        def get(self, model, key):
            return self.quote

    def ex(line, quote=None):
        value = service._quote_fields(line, _S(quote))["rate_ex"]
        return None if value is None else round(float(value), 2)

    base = dict(winning_quote_id=None, tat_days=2)
    check("MRP 1180, 20% off -> net 944 -> ex-GST 800.00",
          ex(SimpleNamespace(mrp=Decimal("1180"), discount_pct=Decimal("20"), net_price=None, **base)) == 800.00)
    check("net price 944 (incl. GST) -> ex-GST 800.00",
          ex(SimpleNamespace(mrp=None, discount_pct=None, net_price=Decimal("944"), **base)) == 800.00)
    check("vendor rate 1180 (incl. GST) -> ex-GST 1000.00",
          ex(SimpleNamespace(mrp=None, discount_pct=None, net_price=Decimal("1180"), winning_quote_id=1, tat_days=1),
             SimpleNamespace(quoted_rate=Decimal("1180"))) == 1000.00)
    check("no price known -> no rate sent",
          ex(SimpleNamespace(mrp=None, discount_pct=Decimal("20"), net_price=None, **base)) is None)

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
