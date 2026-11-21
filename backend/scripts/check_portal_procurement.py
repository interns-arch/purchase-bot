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
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_TMP = Path(tempfile.mkdtemp(prefix="portal-procurement-check-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["ADVANCE_ORDERS_ENABLED"] = "false"
os.environ["PURCHASE_APPROVAL_MODE"] = "true"  # as on the server: must not hold Portal demand
os.environ["PORTAL_PROCUREMENT_ENABLED"] = "true"
os.environ["PORTAL_PROCUREMENT_MODE"] = "live"  # the pilot is checked on its own in [14]
# never the server's real pilot file (its allowed_vendors would keep the test vendors out)
os.environ["PORTAL_PROCUREMENT_PILOT_FILE"] = str(_TMP / "no-pilot.json")
os.environ["PORTAL_PROCUREMENT_ROUNDS"] = "false"  # [1]-[18]: the old timing; [19] checks the 3-hour rounds
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
        self.login_refused = False
        self.headers_seen: list[dict] = []
        self.next_po = 70

    def add_row(self, sol: int, part: str, qty: int, brand: str = "HYUNDAI", po_grp: str | None = None) -> None:
        self.rows[sol] = {
            "row_type": "source", "source_order_line_id": sol, "source_po_group_id": po_grp or f"PO-GRP-{sol}",
            "part_no": part, "brand": brand, "planned_quantity": qty, "procurement_stage": "pending",
            "order_date": "2026-11-10", "buyer_dealer_id": 4587, "editable": True, "is_closed": False,
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = url.path.replace("/api/v1", "")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls[(request.method, path)] += 1
        self.headers_seen.append(dict(request.headers))
        body = json.loads(request.content or b"{}") if request.method == "POST" else {}
        if path == "/auth/login":
            if self.login_refused:
                return httpx.Response(401, json={"detail": "INVALID_CREDENTIALS"})
            return httpx.Response(200, json={"access_token": "jwt-1", "expires_in": 28800})
        if self.unauthorized > 0:
            self.unauthorized -= 1
            return httpx.Response(401, json={"detail": "expired"})
        if path == "/procurement/active" and request.method == "GET":
            items = [r for r in self.rows.values()
                     if (not q.get("part_no") or r["part_no"] == q["part_no"])
                     and (not q.get("po_group_id") or r["source_po_group_id"] == q["po_group_id"])]
            if q.get("include_po_mst") == "true":
                items = items + [{"row_type": "master", "po_mst": a["po_mst"], "po_group_id": a["po_mst"],
                                  "quote_reference": a["quote_reference"], "source_order_line_id": a["sol"],
                                  "allocation_id": a["allocation_id"], "editable": a.get("editable", False),
                                  "supplier_dealer_id": a["dealer"], "planned_quantity": a["qty"]}
                                 for a in self.allocs]
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
                                "quote_reference": body.get("quote_reference"),
                                "sol": body["source_order_line_id"], "allocation_id": 800 + len(self.allocs)})
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
            # Live shape (16 Nov 2026): the status sits under "approval".
            return httpx.Response(200, json={"approval": {"status": self.approval[po],
                                                          "reason": "stock already in transit"}})
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
    cfg.ask_mode = "one"  # sections [1]-[16]: one message; [17] checks step-by-step

    portal = FakePortal()
    client = PortalClient(transport=httpx.MockTransport(portal.handler), sleep=lambda s: None,
                          base_url="https://portal.test/api/v1")
    # the re-check before each vendor question reads the same fake Portal
    service._client = lambda: PortalClient(transport=httpx.MockTransport(portal.handler), sleep=lambda s: None,
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
          and "PO-GRP" not in created[0])
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
    check("purchase team alerted about onboarding", any(to == ALERT and "onboarding needed" in b
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
    print("\n[9] 401/403 are never retried; a refused login blocks all Portal calls")
    from backend.app.portal_procurement import client as portal_client

    portal.unauthorized = 1
    gets = portal.calls[("GET", "/procurement/active")]
    logins = portal.calls[("POST", "/auth/login")]
    try:
        client.get("/procurement/active", {"date_from": "2026-11-05", "date_to": "2026-11-11"})
        check("401 -> AuthStopped", False)
    except AuthStopped:
        check("401 -> AuthStopped", True)
    check("...the request was NOT retried", portal.calls[("GET", "/procurement/active")] == gets + 1)
    check("...and no re-login was attempted", portal.calls[("POST", "/auth/login")] == logins)
    client.get("/procurement/active", {"date_from": "2026-11-05", "date_to": "2026-11-11"})
    check("next run logs in fresh and works", portal.calls[("POST", "/auth/login")] == logins + 1)
    portal.login_refused = True
    portal.unauthorized = 1
    for _ in range(2):
        try:
            client.get("/procurement/active", {})
        except AuthStopped:
            pass
    logins = portal.calls[("POST", "/auth/login")]
    check("refused login -> Portal calls blocked", bool(portal_client.blocked()))
    try:
        client.get("/procurement/active", {})
    except AuthStopped:
        pass
    check("blocked: no further login attempts", portal.calls[("POST", "/auth/login")] == logins)
    portal.login_refused = False
    portal.unauthorized = 0
    portal_client._blocked = None

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
    group_files: list[tuple[str, str, bytes]] = []
    whapi.send_document = (lambda chat, content, name, mime, caption=None, to_vendor=False:
                           group_files.append((chat, name, content)) or f"whapi.f{len(group_files)}")
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

    # ------------------------------------------------------------------ 13
    print("\n[13] vendor-link import: ids not names, existing links win, dry run, idempotent")
    from openpyxl import Workbook

    from backend.app.portal_procurement import links

    with get_session() as s:
        new = {}
        for key, name in (("D", "Delta Parts"), ("E", "Echo Auto"), ("F", "Foxtrot Motors"), ("G", "Golf Spares"),
                          ("H", "Ark Vehicles Private Limited"), ("I", "India Parts"), ("J", "Juliet Auto"),
                          ("K", "Kilo Motors"), ("L", "Lima Spares")):
            v = Vendor(name=name, vendor_code=f"CT_{key}{key}")
            s.add(v)
            s.flush()
            new[key] = v.id
        s.add(am.VendorBrand(brand="HYUNDAI", vendor_id=new["D"], priority=1, discount_type=am.DISC_PERCENT,
                             discount_pct=10))
        s.add(am.AdvanceVendorContact(vendor_id=new["D"], whatsapp_number="919100000004"))
        s.add(DealerPortalVendorMap(vendor_id=new["H"], vendor_name="Ark Vehicles Private Limited", dp_dealer_id=1709,
                                    status=DealerPortalVendorMapStatus.CONFIRMED))
        links.refresh_dealers(s, [
            {"dealer_id": 40, "dealer_name": "Delta Parts"}, {"dealer_id": 41, "dealer_name": "Echo Auto Pvt"},
            {"dealer_id": 42, "dealer_name": "Foxtrot Motors"}, {"dealer_id": 43, "dealer_name": "FOXTROT MOTORS"},
            {"dealer_id": 44, "dealer_name": "Kilo Motors"}, {"dealer_id": 14, "dealer_name": "Ark Automotive"},
            {"dealer_id": 1709, "dealer_name": "Ark Vehicles Private Limited"}, {"dealer_id": 27, "dealer_name": "Alpha"},
            {"dealer_id": 28, "dealer_name": "Beta"}], "HYUNDAI")
        map_before = sorted((x.vendor_id, x.dp_dealer_id, x.status.value)
                            for x in s.execute(select(DealerPortalVendorMap)).scalars())

    # A Portal line whose fastest vendor (Delta) is NOT linked yet -> onboarding needed.
    portal.add_row(12481, "86522C9000", 1)
    portal.options[12481] = [{"dealer_id": 27}, {"dealer_id": 40, "dealer_name": "Delta Parts"}]
    run_poll()
    reply("919100000004", "haan aaj")
    reply("919100000001", "86522C9000 hai 5 din")  # Alpha has another open question too
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    run_steps(2)
    a = allocs(12481)
    check("unlinked winner -> onboarding needed, quote saved", len(a) == 1 and a[0].vendor_id == new["D"]
          and a[0].state == m.VENDOR_ONBOARDING_REQUIRED and a[0].quoted_tat_days == 0)
    check("...not given to the linked runner-up", not any(p["source_order_line_id"] == 12481 for p in portal.payloads))

    sheet = _TMP / "links.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Vendor ID", "Code", "Bot vendor", "Suggested dealer ID", "Bot verdict",
               "YOUR ANSWER (Y = suggested is right / dealer ID / N = not on Portal)"])
    rows = [
        (vendors["A"], "Alpha Motors", 99, "LIKELY", "Y"),        # linked (27): suggestion ignored
        (new["H"], "Ark Vehicles Private Limited", 14, "ALREADY LINKED", "Y"),  # stays 1709
        (new["D"], "Delta Parts", 40, "LIKELY", "Y"),             # new link by Y
        (new["E"], "Echo Auto", 41, "LIKELY", ""),                # names differ -> not auto
        (new["F"], "Foxtrot Motors", 42, "LIKELY", ""),           # twin Portal name -> not auto
        (new["G"], "Golf Spares", 777, "NO MATCH", "777"),        # unknown dealer id -> rejected
        (new["L"], "Lima Spares", 41, "CHECK", "41"),             # typed id -> link
        (new["I"], "India Parts", 1, "NO MATCH", "N"),            # onboarding
        (vendors["B"], "Beta Spares", 1, "LIKELY", "N"),          # N never deletes a link
        (new["J"], "Juliet Auto", 44, "CHECK", ""),               # CHECK never auto-accepted
        (new["K"], "Kilo Motors", 44, "LIKELY", ""),              # exact name -> auto with --accept-exact
    ]
    for vid, name, sug, verdict, ans in rows:
        ws.append([vid, "", "RENAMED IN SHEET " + name, sug, verdict, ans])  # names in the sheet are ignored
    wb.save(sheet)

    with get_session() as s:
        steps = {x.vendor_id: x for x in links.plan(links.read_sheet(str(sheet)), s, accept_exact=True)}
        s.rollback()
    act = lambda key: steps[vendors[key] if key in vendors else new[key]].action  # noqa: E731
    check("linked vendor: Y suggestion ignored, existing kept", act("A") == links.KEEP)
    check("vendor like 56: stays on 1709, never 14", act("H") == links.KEEP and steps[new["H"]].old_dealer == 1709)
    check("Y links an unlinked vendor", act("D") == links.LINK and steps[new["D"]].proposed == 40)
    check("names differ -> not auto-accepted", act("E") == links.REJECT)
    check("two Portal dealers with that name -> not auto-accepted", act("F") == links.REJECT)
    check("typed dealer id not in the Portal table -> rejected", act("G") == links.REJECT)
    check("typed dealer id that exists -> linked", act("L") == links.LINK and steps[new["L"]].proposed == 41)
    check("N -> onboarding required", act("I") == links.ONBOARD)
    check("N on a linked vendor keeps the link", act("B") == links.KEEP)
    check("CHECK row blank -> no change", act("J") == links.NOCHANGE)
    check("exact-name LIKELY row auto-accepted", act("K") == links.LINK and steps[new["K"]].proposed == 44)
    with get_session() as s:
        check("dry run saved nothing", not s.execute(select(m.PortalVendorLink)).first())

    with get_session() as s:
        done = links.apply(links.plan(links.read_sheet(str(sheet)), s, accept_exact=True), s,
                           source="links.xlsx", decided_by="check")
    check("commit: 3 linked, 1 onboarding", done == {links.LINK: 3, links.ONBOARD: 1})
    with get_session() as s:
        again = links.plan(links.read_sheet(str(sheet)), s, accept_exact=True)
        done2 = links.apply(again, s, source="links.xlsx", decided_by="check")
        map_after = sorted((x.vendor_id, x.dp_dealer_id, x.status.value)
                           for x in s.execute(select(DealerPortalVendorMap)).scalars())
        check("idempotent: second import changes nothing", done2 == {links.LINK: 0, links.ONBOARD: 0})
        check("stock-upload map untouched", map_before == map_after)
        check("resolve(56-like) == 1709", links.resolve(new["H"], s) == 1709)

    run_steps(2)
    a = allocs(12481)
    check("after linking, the saved quote becomes a PO-MST by itself",
          a[0].state == m.PO_MST_CREATED and a[0].supplier_dealer_id == 40)

    # ------------------------------------------------------------------ 14
    print("\n[14] pilot mode: only the chosen line, only the chosen vendor")
    from backend.app.portal_procurement.config import settings as portal_settings

    portal_settings.mode = "pilot"
    portal_settings.pilot_file = str(_TMP / "pilot.json")
    portal_settings.pilot_lines = set()
    portal.add_row(12482, "86523C9000", 1)
    portal.add_row(12483, "86524C9000", 1)
    portal.options[12482] = [{"dealer_id": 27}]
    before = (len(sent), len(group_posts))
    run_poll()
    check("pilot with no lines chosen: nobody asked", (len(sent), len(group_posts)) == before)
    (_TMP / "pilot.json").write_text(json.dumps({"parts": ["86523-C9000"], "vendors": [vendors["B"]]}))
    run_poll()
    check("only the pilot line is enquired", demand(12482).state == m.ENQUIRY_SCHEDULED
          and demand(12483).state == m.DISCOVERED)
    check("only the pilot vendor is asked", any(to == "919100000002" and "86523C9000" in b for to, b in sent)
          and not any("86523C9000" in b for to, b in sent if to != "919100000002")
          and not any("86523C9000" in b for _, b in group_posts))
    check("pilot picked the line by PART NUMBER (a fresh advance order)", demand(12482).advance_order_id is not None)
    portal.add_row(12484, "86525C9000", 1)
    portal.add_row(12485, "86525C9000", 1)
    (_TMP / "pilot.json").write_text(json.dumps({"parts": ["86525C9000"], "vendors": [vendors["B"]],
                                                 "skip_lines": [12484]}))
    run_poll()
    check("skip_lines: an older line of a pilot part is not asked", demand(12484).state == m.DISCOVERED
          and demand(12485).state == m.ENQUIRY_SCHEDULED)
    portal_settings.mode = "live"

    # ------------------------------------------------------------------ 15
    print("\n[15] po_confirmed source lines are workable; a live PO-MST allocation is superseded")

    def payload_for(sol):
        return next((p for p in reversed(portal.payloads) if p.get("source_order_line_id") == sol), None)

    def win_with_alpha(sol):
        run_poll()
        reply("919100000001", "haan 1 din")
        reply("919100000002", "nahi")
        reply("919100000003", "nahi")
        run_steps(3)

    portal.add_row(12490, "86530C9000", 1)
    portal.rows[12490].update(procurement_stage="po_confirmed", supplier_dealer_id=21)
    portal.add_row(12491, "86531C9000", 1)
    portal.rows[12491].update(procurement_stage="po_confirmed", editable=False)
    portal.add_row(12493, "86533C9000", 1)
    portal.rows[12493].update(procurement_stage="received")
    for sol in (12490, 12491, 12492, 12493):
        portal.options[sol] = [{"dealer_id": 27}, {"dealer_id": 28}]
    win_with_alpha(12490)
    with get_session() as s:
        known = {d.source_order_line_id for d in s.execute(select(m.PortalDemandLine)).scalars()}
    check("po_confirmed + editable line is picked up", 12490 in known)
    check("editable=false line is ignored", 12491 not in known)
    check("other stages are ignored", 12493 not in known)
    p = payload_for(12490) or {}
    check("no live PO-MST allocation -> allocate WITHOUT allocation_id",
          bool(p) and "allocation_id" not in p and p.get("supplier_dealer_id") == 27)
    check("...and a PO-MST was created", allocs(12490)[0].state in (m.PO_MST_CREATED, m.COMPLETED,
                                                                   m.KNOCKOFF_PREVIEWED, m.SAFE_KNOCKOFF_COMMITTED)
          or demand(12490).state == m.COMPLETED)

    portal.add_row(12492, "86532C9000", 1)
    portal.rows[12492].update(procurement_stage="po_confirmed", supplier_dealer_id=21)
    portal.allocs.append({"po_mst": "PO-MST-2026-0876", "dealer": 21, "qty": 1, "quote_reference": "OLD",
                          "sol": 12492, "allocation_id": 876, "editable": True})
    win_with_alpha(12492)
    p = payload_for(12492) or {}
    check("live editable PO-MST allocation -> allocate WITH its allocation_id (supersede)",
          p.get("allocation_id") == 876 and p.get("supplier_dealer_id") == 27)

    # re-fetch guard: the part changed on the Portal between the quote and the write
    portal.add_row(12494, "86534C9000", 1)
    portal.options[12494] = [{"dealer_id": 27}]
    run_poll()
    reply("919100000001", "haan 1 din")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    portal.rows[12494]["part_no"] = "99999X0000"
    n_before = len(portal.payloads)
    run_steps(3)
    check("part changed before the write -> nothing allocated", len(portal.payloads) == n_before
          and allocs(12494) and allocs(12494)[0].state == m.ALLOCATION_CANCELLED)

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

    # ------------------------------------------------------------------ 16
    print("\n[16] enquiry asks MRP, discount and days; the answers reach the Portal")
    portal.add_row(12495, "86535C9000", 1)
    portal.options[12495] = [{"dealer_id": 27}, {"dealer_id": 28}]
    n_sent = len(sent)
    run_poll()
    run_steps(1)
    question = next((b for _to, b in sent[n_sent:] + group_posts if "86535C9000" in b), "")
    check("the question asks MRP, discount and days",
          "MRP" in question and "Discount" in question and "Kitne din" in question)
    reply("919100000001", "MRP 1180, 25%, 1 din")
    reply("919100000002", "haan mrp 1180 discount 10, 1 din")
    reply("919100000003", "1180 30 1")  # bare numbers: never guessed
    run_steps(3)
    p = payload_for(12495) or {}
    check("same speed -> the bigger stated discount (25% beats 10% and the standing 20%) wins",
          p.get("supplier_dealer_id") == 27)
    check("Portal gets the vendor's MRP", float(p.get("quoted_mrp_incl_gst") or 0) == 1180.0)
    check("Portal gets the vendor's stated discount, not the standing one", float(p.get("quoted_discount_percent") or 0) == 25.0)
    check("Portal gets the days", p.get("quoted_tat_days") == 1)
    check("rate = MRP 1180 x 75% / 1.18 = 750 ex-GST", round(float(p.get("quoted_rate_ex_gst") or 0), 2) == 750.00)
    with get_session() as s:
        gamma_quote = s.execute(select(am.AdvanceVendorQuote).where(
            am.AdvanceVendorQuote.vendor_id == vendors["C"],
            am.AdvanceVendorQuote.mrp == Decimal("1180"))).first()
    check("bare numbers '1180 30 1' are not read as MRP/discount", gamma_quote is None)

    # ------------------------------------------------------------------ 17
    print("\n[17] step by step: pcs, then MRP, then discount, then days")
    cfg.ask_mode = "steps"
    portal.add_row(12496, "86536C9000", 2)
    portal.options[12496] = [{"dealer_id": 27}, {"dealer_id": 28}]
    mark = len(sent) + len(group_posts)

    def said_since():
        return [b for _t, b in (sent + group_posts)[mark:]]

    run_poll()
    run_steps(1)
    first = [b for b in said_since() if "86536C9000" in b]
    check("first question is only 'Sir ye part hai kya with pcs?'",
          bool(first) and first[0].startswith("Sir ye part hai kya with pcs?") and "MRP" not in first[0])
    mark = len(sent) + len(group_posts)
    reply("919100000001", "haan 2 pcs")
    check("after pcs -> asks MRP", any("MRP kitna hai" in b for b in said_since()))
    reply("919100000002", "nahi")  # Beta: not available -> no more questions to him
    reply("919100000003", "nahi")
    mark = len(sent) + len(group_posts)
    reply("919100000001", "3589")
    check("after MRP -> asks discount", any("discount kitna denge" in b for b in said_since()))
    mark = len(sent) + len(group_posts)
    reply("919100000001", "kya?")
    check("unclear answer -> the same question once more", any("samajh nahi aaya" in b and "discount" in b
                                                               for b in said_since()))
    mark = len(sent) + len(group_posts)
    reply("919100000001", "10")
    check("after discount -> asks days", any("kitne din" in b for b in said_since()))
    check("nothing allocated before the last answer", payload_for(12496) is None)
    reply("919100000001", "3 din")
    run_steps(3)
    p = payload_for(12496) or {}
    check("after days -> PO-MST with Alpha", p.get("supplier_dealer_id") == 27)
    check("Portal gets MRP 3589, 10%, 3 days",
          float(p.get("quoted_mrp_incl_gst") or 0) == 3589.0 and float(p.get("quoted_discount_percent") or 0) == 10.0
          and p.get("quoted_tat_days") == 3)
    check("rate = 3589 x 90% / 1.18 = 2737.37", round(float(p.get("quoted_rate_ex_gst") or 0), 2) == 2737.37)

    # all at once at the first step: no further questions
    portal.add_row(12497, "86537C9000", 1)
    portal.options[12497] = [{"dealer_id": 27}]
    run_poll()
    run_steps(1)
    mark = len(sent) + len(group_posts)
    s_mark, g_mark = len(sent), len(group_posts)
    reply("919100000001", "haan 1 pcs, MRP 900, 12%, 2 din")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")

    def asked_after():
        return [(to, b) for to, b in sent[s_mark:] + group_posts[g_mark:] if "Sir," in b]

    got = asked_after()
    check("everything in one answer -> no more questions to him; the 'nahi' vendors are asked the ETA",
          sorted(to for to, _ in got) == ["919100000002", "919100000003"]
          and all("kab tak aa jayega" in b and "86537C9000" in b for _, b in got))
    check("...ETA question spelt right",
          bool(got) and got[0][1].startswith("Sir, abhi agar nahi hai to kab tak aa jayega? ETA bata do (kitne din mein)."))
    reply("919100000002", "nahi aayega")
    reply("919100000003", "pata nahi")
    check("...'nahi aayega' / 'pata nahi' -> nothing more asked", len(asked_after()) == 2)
    run_steps(3)
    p = payload_for(12497) or {}
    check("...and the Portal gets MRP 900, 12%, 2 days",
          float(p.get("quoted_mrp_incl_gst") or 0) == 900.0 and float(p.get("quoted_discount_percent") or 0) == 12.0
          and p.get("quoted_tat_days") == 2)

    # "nahi" -> "kab tak aa jayega?" -> "4 din": counted as available in 4 days
    portal.add_row(12499, "86539C9000", 1)
    portal.options[12499] = [{"dealer_id": 27}]
    run_poll()
    run_steps(1)
    mark = len(sent) + len(group_posts)
    reply("919100000001", "nahi hai")
    reply("919100000001", "4 din")
    nxt = [b for b in said_since() if "Sir," in b]
    check("'nahi' -> ETA asked; '4 din' -> then MRP is asked (not days again)",
          len(nxt) == 2 and "kab tak aa jayega" in nxt[0] and "MRP" in nxt[1])
    reply("919100000001", "800")
    reply("919100000001", "10")
    reply("919100000002", "nahi")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    reply("919100000003", "nahi")
    run_steps(3)
    p = payload_for(12499) or {}
    check("...the part coming in 4 days goes to him: MRP 800, 10%, 4 days",
          p.get("supplier_dealer_id") == 27 and float(p.get("quoted_mrp_incl_gst") or 0) == 800.0
          and float(p.get("quoted_discount_percent") or 0) == 10.0 and p.get("quoted_tat_days") == 4)

    # vendor stops after MRP: his time runs out -> partial answer kept
    portal.add_row(12498, "86538C9000", 1)
    portal.options[12498] = [{"dealer_id": 27}]
    run_poll()
    run_steps(1)
    reply("919100000001", "haan")
    reply("919100000001", "mrp 500")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    run_steps(8)
    p = payload_for(12498) or {}
    check("went quiet after MRP -> still allocated with MRP 500 and the standing 20%",
          p.get("supplier_dealer_id") == 27 and float(p.get("quoted_mrp_incl_gst") or 0) == 500.0
          and float(p.get("quoted_discount_percent") or 0) == 20.0)

    # ------------------------------------------------------------------ 18
    print("\n[18] minimum margin 2% of MRP over what the customer pays")
    cfg.ask_mode = "one"
    portal_settings.min_margin_percent = 2
    portal.add_row(12501, "86541C9000", 1)
    portal.rows[12501].update(mrp="1000.00", customer_discount_percent="15.00", price="850.00")
    portal.options[12501] = [{"dealer_id": 27}, {"dealer_id": 28}]
    run_poll()
    run_steps(1)
    reply("919100000001", "haan 1 din, 16%")   # fastest, but keeps only 1%
    reply("919100000002", "haan 2 din, 20%")   # slower, keeps 5%
    reply("919100000003", "nahi")
    run_steps(3)
    p = payload_for(12501) or {}
    with get_session() as s:
        d = s.execute(select(m.PortalDemandLine).where(m.PortalDemandLine.source_order_line_id == 12501)).scalar_one()
        stored = (d.portal_mrp, d.customer_discount_pct, d.sale_price)
    check("customer MRP, discount and price are stored from the Portal row",
          stored == (Decimal("1000"), Decimal("15"), Decimal("850")))
    check("fastest vendor below the margin loses; the next one that keeps it wins",
          p.get("supplier_dealer_id") == 28 and float(p.get("quoted_discount_percent") or 0) == 20.0)

    portal.add_row(12502, "86542C9000", 1)
    portal.rows[12502].update(mrp="1000.00", customer_discount_percent="19.00", price="810.00")
    portal.options[12502] = [{"dealer_id": 27}, {"dealer_id": 28}]
    run_poll()
    run_steps(1)
    reply("919100000001", "haan 1 din, 20%")   # keeps only 1%
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    run_steps(3)
    check("nobody keeps the margin -> no PO-MST", payload_for(12502) is None)
    check("...and the line is held for the team", demand(12502).state == m.MARGIN_HOLD
          and "margin below 2" in (demand(12502).note or ""))

    portal_settings.min_margin_percent = 0
    portal.add_row(12503, "86543C9000", 1)
    portal.rows[12503].update(mrp="1000.00", customer_discount_percent="19.00", price="810.00")
    portal.options[12503] = [{"dealer_id": 27}]
    run_poll()
    run_steps(1)
    reply("919100000001", "haan 1 din, 20%")
    reply("919100000002", "nahi")
    reply("919100000003", "nahi")
    run_steps(3)
    check("margin 0 = rule off: the same quote goes through", (payload_for(12503) or {}).get("supplier_dealer_id") == 27)

    # ------------------------------------------------------------------ 19
    print("\n[19] 3-hour rounds: brand only, groups only, 7 days + margin, ties, Prateek sir, delivery record")
    from backend.app.portal_procurement import live as live_view, rounds

    PRATEEK = "919000000077"
    portal_settings.rounds = True
    portal_settings.mode = "live"
    portal_settings.min_margin_percent = 2
    portal_settings.round_hours, portal_settings.max_asks, portal_settings.max_tat_days = 3, 3, 7
    portal_settings.round_start, portal_settings.round_end = "10:00", "19:00"
    check("round times: 10:00, 13:00, 16:00, 19:00",
          [x.strftime("%H:%M") for x in rounds.slots_for(date(2026, 11, 20))] == ["10:00", "13:00", "16:00", "19:00"])
    check("a round missed overnight is not run in the morning",
          rounds.current_slot(datetime(2026, 11, 20, 9, 30)) is None
          and rounds.current_slot(datetime(2026, 11, 20, 23, 0)) is None
          and rounds.current_slot(datetime(2026, 11, 20, 19, 5)) == datetime(2026, 11, 20, 19, 0))
    portal_settings.round_start, portal_settings.round_end = "09:00", "21:00"  # the times used below
    cfg.escalation_numbers = [PRATEEK]
    cfg.ask_mode = "one"
    rounds.now_ist_naive = now
    groups = {"CT_A": "120363000000000001@g.us", "CT_B": "120363000000000002@g.us", "CT_T": "120363000000000009@g.us"}
    whapi.vendor_groups = lambda: groups
    with get_session() as s:
        tata = Vendor(name="Tata Only", vendor_code="CT_T")
        s.add(tata)
        s.flush()
        s.add(am.VendorBrand(brand="TATA", vendor_id=tata.id, priority=1, discount_type=am.DISC_PERCENT, discount_pct=10))
        s.add(am.AdvanceVendorContact(vendor_id=tata.id, whatsapp_number="919100000009"))

    def run_round():
        with get_session() as s:
            service.poll(s, client, clock["now"].date())
            s.commit()
            slot = rounds.round_due(s, clock["now"])
            if slot is not None:
                rounds.mark_round(s, slot, service.enquire(s))

    def settle(times: int = 3):
        for _ in range(times):
            with get_session() as s:
                engine.tick(s, clock["now"])
            with get_session() as s:
                service.run_steps(s, client, clock["now"].date())
            with get_session() as s:
                for row in s.execute(select(m.PortalAllocation)).scalars():
                    row.next_try_at = None
                for row in s.execute(select(m.PortalPoMaster)).scalars():
                    row.next_try_at = None

    def posts(group: str, part: str) -> int:
        return sum(1 for c, b in group_posts if c == group and part in b)

    def to_prateek() -> list[str]:
        return [b for to, b in sent if to == PRATEEK]

    def cust(sol, part, mrp="1000.00", disc="10.00", price="900.00", brand="HYUNDAI"):
        portal.add_row(sol, part, 1, brand=brand)
        portal.rows[sol].update(mrp=mrp, customer_discount_percent=disc, price=price)
        portal.options[sol] = [{"dealer_id": 27}, {"dealer_id": 28}]

    GA, GB, GT = groups["CT_A"], groups["CT_B"], groups["CT_T"]
    clock["now"] = datetime(2026, 11, 20, 9, 5)
    group_posts.clear()
    gamma_before = asked("919100000003")
    cust(12601, "R19A01")
    cust(12602, "R19T01", brand="TATA")
    run_round()
    check("round: Hyundai part posted in Alpha's and Beta's groups", posts(GA, "R19A01") == 1 and posts(GB, "R19A01") == 1)
    check("round: Gamma (no group) not asked, not even one to one", asked("919100000003") == gamma_before)
    check("brand only: the Tata part goes only to the Tata vendor",
          posts(GT, "R19T01") == 1 and posts(GA, "R19T01") == 0 and posts(GB, "R19T01") == 0 and posts(GT, "R19A01") == 0)

    clock["now"] = datetime(2026, 11, 20, 10, 0)
    cust(12603, "R19A03")
    run_round()
    check("between rounds a new line waits", demand(12603).state == m.DISCOVERED and posts(GA, "R19A03") == 0)

    # only reply is over 7 days -> no winner; the silent vendor is asked again next round
    reply("919100000001", "haan 10 din, 20%")
    clock["now"] = datetime(2026, 11, 20, 12, 10)
    run_round()
    settle()
    check("a reply over 7 days does not win", payload_for(12601) is None and demand(12601).state != m.WINNER_SELECTED)
    check("the silent vendor gets the part again after 3 hours", posts(GB, "R19A01") == 2)
    check("...and the waiting line went out in this round", posts(GA, "R19A03") == 1)
    reply("919100000002", "R19A01 haan 3 din, 15%")  # two lists open -> he names the part
    settle()
    check("a 3-day reply with 5% margin then wins", (payload_for(12601) or {}).get("supplier_dealer_id") == 28)

    # tie: same days, same discount -> better delivery record wins
    with get_session() as s:
        d1 = s.execute(select(m.PortalDemandLine).where(m.PortalDemandLine.source_order_line_id == 12601)).scalar_one()
        for i, (vid, late) in enumerate(((vendors["A"], True), (vendors["B"], False))):
            s.add(m.PortalAllocation(demand_line_id=d1.id, vendor_id=vid, quantity=1, state=m.COMPLETED,
                                     quote_reference=f"HIST-{i}", idempotency_key=f"hist-{i}",
                                     promised_date=date(2026, 11, 1),
                                     received_at=datetime(2026, 11, 5 if late else 1, 12, 0)))
    clock["now"] = datetime(2026, 11, 20, 15, 5)
    cust(12604, "R19A04")
    run_round()
    reply("919100000001", "haan 2 din, 15%")
    reply("919100000002", "haan 2 din, 15%")
    settle()
    check("tie on days and discount: the vendor who delivers on time wins",
          (payload_for(12604) or {}).get("supplier_dealer_id") == 28)

    # every reply below the margin -> Prateek sir chooses
    clock["now"] = datetime(2026, 11, 21, 9, 5)
    cust(12605, "R19A05", disc="19.00", price="810.00")
    run_round()
    reply("919100000001", "haan 1 din, 18%")
    reply("919100000002", "haan 2 din, 19%")
    settle()
    with get_session() as s:
        d5 = s.execute(select(m.PortalDemandLine).where(m.PortalDemandLine.source_order_line_id == 12605)).scalar_one()
        code5 = f"C{d5.id}"
    msg = next((b for b in to_prateek() if code5 in b), "")
    check("nobody keeps 2% -> Prateek sir gets every answer, numbered", "1. Alpha Motors" in msg and "2. Beta Spares" in msg
          and "margin" in msg and payload_for(12605) is None)
    with get_session() as s:
        check("his reply picks the vendor", rounds.handle_reply(PRATEEK, f"{code5} 2", s))
    settle()
    check("...and the PO-MST goes to the vendor he chose", (payload_for(12605) or {}).get("supplier_dealer_id") == 28)

    cust(12606, "R19A06", disc="19.00", price="810.00")
    clock["now"] = datetime(2026, 11, 21, 12, 5)
    run_round()
    reply("919100000001", "haan 1 din, 18%")
    reply("919100000002", "nahi")
    settle()
    with get_session() as s:
        check("a bare 'cancel' works when only one choice waits", rounds.handle_reply(PRATEEK, "cancel", s))
    check("...and the line is closed", demand(12606).state == m.CLOSED and payload_for(12606) is None)

    # nobody replies: asked 3 times, 3 hours apart, then Prateek sir is told and asking stops
    clock["now"] = datetime(2026, 11, 22, 9, 5)
    cust(12607, "R19A07")
    run_round()
    for hour in (12, 15, 18):
        clock["now"] = datetime(2026, 11, 22, hour, 10)
        settle(1)
    clock["now"] = datetime(2026, 11, 22, 18, 20)
    settle(1)
    check("silent vendor asked 3 times in all", posts(GA, "R19A07") == 3 and posts(GB, "R19A07") == 3)
    check("Prateek sir told who did not reply", any("3 baar" in b and "R19A07" in b and "Alpha Motors" in b
                                                    for b in to_prateek()))
    clock["now"] = datetime(2026, 11, 23, 12, 10)
    settle(2)
    check("...and nobody is asked a 4th time", posts(GA, "R19A07") == 3)
    ao7 = f"AO-{demand(12607).advance_order_id}"
    check("19 Nov: after 6 hours with no answer Prateek sir is asked (new vendor / cancel)",
          any("6 ghante" in b and "R19A07" in b and f"{ao7} vendor" in b and f"{ao7} cancel" in b for b in to_prateek()))
    from backend.app.advance_orders import escalation as esc

    with get_session() as s:
        check("...his cancel is taken", esc.handle_reply(PRATEEK, f"{ao7} cancel", s))
    settle(1)
    check("the line ends as no vendor", demand(12607).state == m.NO_VENDOR)

    # today's stock sheet shows the part: that vendor is not asked; his sheet is his reply
    from core.models import ImportStatus, InventoryImport, VendorInventory

    clock["now"] = datetime(2026, 11, 24, 9, 5)
    with get_session() as s:
        imp = InventoryImport(vendor_id=vendors["A"], file_name="alpha stock 24.11.xlsx", file_size_bytes=10,
                              content_hash="h-24", status=ImportStatus.COMPLETED, is_active=True,
                              completed_at=clock["now"] - timedelta(minutes=30))
        for old_imp in s.execute(select(InventoryImport).where(InventoryImport.vendor_id == vendors["A"],
                                                               InventoryImport.is_active.is_(True))).scalars():
            old_imp.is_active = False
        s.flush()
        s.add(imp)
        s.flush()
        s.add(VendorInventory(vendor_id=vendors["A"], import_id=imp.id, row_number=2, vendor_part_number="R19A08",
                              normalized_part_number="R19A08", quantity_available=5, mrp=1000, price=800,
                              raw_data={"Part": "R19A08", "TAT": "1 day"}))
        # a second part: in stock, but no price and no days on the sheet
        s.add(VendorInventory(vendor_id=vendors["A"], import_id=imp.id, row_number=3, vendor_part_number="R19A10",
                              normalized_part_number="R19A10", quantity_available=2, mrp=1000, price=None,
                              raw_data={"Part": "R19A10"}))
    cust(12608, "R19A08")
    run_round()
    check("stock-sheet vendor is not asked", posts(GA, "R19A08") == 0)
    check("...the others are", posts(GB, "R19A08") == 1)
    settle(1)
    check("no winner before the others answer or the round ends", payload_for(12608) is None)
    reply("919100000002", "R19A08 haan 2 din, 25%")
    settle()
    p8 = payload_for(12608) or {}
    check("the sheet (1 day, 20% from MRP 1000 / price 800) beats a 2-day reply",
          p8.get("supplier_dealer_id") == 27 and p8.get("quoted_tat_days") == 1
          and float(p8.get("quoted_discount_percent") or 0) == 20.0)

    # in stock on the sheet, but no price and no days: asked ONLY the discount, then the days
    cust(12610, "R19A10")
    group_posts.clear()
    clock["now"] = datetime(2026, 11, 24, 12, 5)
    run_round()
    a10 = [b for c, b in group_posts if c == GA and "R19A10" in b]
    check("sheet without price / days: not asked if he has it, asked the discount",
          len(a10) == 1 and "discount" in a10[0].lower() and "pcs" not in a10[0].lower())
    reply("919100000001", "12")
    settle(1)
    a10 = [b for c, b in group_posts if c == GA and "R19A10" in b]
    check("...then only the days", len(a10) == 2 and "din" in a10[1].lower()
          and "mrp" not in " ".join(a10).lower())
    reply("919100000001", "1 din")
    reply("919100000002", "R19A10 haan 3 din, 10%")
    settle()
    p10 = payload_for(12610) or {}
    check("...and his sheet + answers win (1 day, 12%)", p10.get("supplier_dealer_id") == 27
          and p10.get("quoted_tat_days") == 1 and float(p10.get("quoted_discount_percent") or 0) == 12.0)
    check("rounds._sheet_days reads a days column", rounds._sheet_days({"Lead Time (days)": "4"}) == 4
          and rounds._sheet_days({"Part": "X1", "Pending": "7"}) is None)
    with get_session() as s:
        imp_row = s.execute(select(InventoryImport).where(InventoryImport.content_hash == "h-24")).scalar_one()
        imp_row.completed_at = datetime(2026, 11, 23, 18, 0)
    clock["now"] = datetime(2026, 11, 24, 15, 5)
    cust(12609, "R19A08")
    portal.rows[12609]["part_no"] = "R19A08"
    run_round()
    check("an older sheet does not count: that vendor is asked", posts(GA, "R19A08") == 1)

    # delivery record: promised date at PO-MST, received when the Portal shows it
    a601 = [x for x in allocs(12601) if x.quote_reference.startswith("PH-Q-")]
    check("promised date kept when the PO-MST is made", bool(a601) and a601[0].promised_date is not None)
    portal.rows[12601]["qty_received"] = 1
    run_round()
    a601 = [x for x in allocs(12601) if x.quote_reference.startswith("PH-Q-")]
    check("received day recorded from the Portal row", bool(a601) and a601[0].received_at is not None)

    with get_session() as s:
        view = live_view.live(s)
    hy = next((b for b in view["brands"] if b["brand"] == "HYUNDAI"), {})
    check("live view: brands with vendors and groups", hy.get("vendors", 0) >= 3 and hy.get("in_group") == 2
          and "Gamma Auto" in hy.get("no_group_names", []))
    check("live view: parts listed with asked / replied", any(x["part_no"] == "R19A04" and x["replied"] == 2
                                                              for x in view["lines"]))
    # Beta: the on-time history row + 12601, received after the Portal's arrival date -> 50%.
    check("live view: Beta's on-time record shown", any(v["vendor"] == "Beta Spares" and v["on_time_pct"] == 50
                                                        and v["delivered"] == 2 for v in view["vendors"]))
    # ------------------------------------------------------------------ 20
    print("\n[20] several parts: question + enquiry CSV; the filled CSV is the answer")
    from backend.app.advance_orders import enquiry_csv

    cfg.ask_mode = "steps"
    clock["now"] = datetime(2026, 11, 25, 9, 5)
    group_posts.clear()
    group_files.clear()
    cust(12701, "R20A01")
    cust(12702, "R20A02")
    run_round()
    a_post = [b for c, b in group_posts if c == GA and "R20A01" in b]
    a_file = [(n, body) for c, n, body in group_files if c == GA]
    check("several parts: one question in his group, naming the CSV",
          len(a_post) == 1 and "R20A02" in a_post[0] and "CSV" in a_post[0])
    head = a_file[0][1].decode("utf-8-sig").splitlines() if a_file else []
    check("...and the CSV: Part No, Qty Needed (filled), Available Qty, Yes/No, Discount %, Days",
          bool(head) and head[0].split(",") == enquiry_csv.HEADERS and head[1].startswith("R20A01,1,")
          and "PO-" not in a_file[0][0].upper())
    filled = _TMP / "filled.csv"
    filled.write_text("Part No,Qty Needed,Available Qty,Available (Yes/No),Discount %,Days (TAT)\n"
                      "R20A01,1,1,Yes,15,\nR20A02,1,0,No,,\n", encoding="utf-8")
    other = _TMP / "stock.csv"
    other.write_text("Part,Stock\nR20A01,5\n", encoding="utf-8")
    before_posts = len(group_posts)
    with get_session() as s:
        check("a stock sheet that is not the enquiry CSV is not taken",
              not engine.handle_enquiry_file("919100000001", other, "stock.csv", s, clock["now"]))
    with get_session() as s:
        check("the filled CSV is taken as his answer",
              engine.wants_enquiry_file("919100000001", "filled.csv", s)
              and engine.handle_enquiry_file("919100000001", filled, "filled.csv", s, clock["now"]))
    new_posts = [b for c, b in group_posts[before_posts:] if c == GA]
    check("...only the missing thing is asked: days, for the part he has (no MRP, no discount)",
          len(new_posts) == 1 and "R20A01" in new_posts[0] and "din" in new_posts[0]
          and "R20A02" not in new_posts[0] and "mrp" not in new_posts[0].lower()
          and "discount" not in new_posts[0].lower())
    later_mark = len(group_posts)
    reply("919100000001", "2 din")
    later_q = [b for c, b in group_posts[later_mark:] if c == GA]
    check("...then the part he does not have: when will it come",
          len(later_q) == 1 and "R20A02" in later_q[0] and "kab tak aa jayega" in later_q[0]
          and "R20A01" not in later_q[0])
    reply("919100000001", "nahi aayega")
    reply("919100000002", "nahi")
    reply("919100000002", "nahi")
    settle()
    p701 = payload_for(12701) or {}
    check("...and the CSV answer wins: Alpha, 15%, 2 days",
          p701.get("supplier_dealer_id") == 27 and p701.get("quoted_tat_days") == 2
          and float(p701.get("quoted_discount_percent") or 0) == 15.0)
    check("R20A02 (No in the CSV) is not given to Alpha", (payload_for(12702) or {}).get("supplier_dealer_id") != 27)
    cust(12703, "R20A03")
    clock["now"] = datetime(2026, 11, 25, 12, 5)
    group_files.clear()
    run_round()
    check("one part: asked as before, no CSV", not group_files
          and any("R20A03" in b and "CSV" not in b for c, b in group_posts if c == GA))
    # ------------------------------------------------------------------ 21
    print("\n[21] 19 Nov meeting: Portal discount not asked, priority, low-discount alert, new vendor, report")
    from backend.app.advance_orders import escalation as esc
    from backend.app.portal_procurement import reporting

    cfg.ask_mode = "steps"
    rounds._dealer_discounts.clear()

    def round_with_portal():
        with get_session() as s:
            service.poll(s, client, clock["now"].date())
            s.commit()
            slot = rounds.round_due(s, clock["now"])
            if slot is not None:
                rounds.mark_round(s, slot, service.enquire(s, client))

    # Portal has 15% set for Alpha (dealer 27), nothing (0) for Beta (dealer 28)
    clock["now"] = datetime(2026, 12, 1, 9, 5)
    cust(12801, "R21A01", disc="5.00", price="950.00")
    portal.options[12801] = [{"dealer_id": 27, "discount_percent": "15.00"}, {"dealer_id": 28, "discount_percent": "0"}]
    group_posts.clear()
    round_with_portal()
    settle(1)
    check("Portal vendor discounts read for the line",rounds.dealer_discount(12801, 27) == 15
          and rounds.dealer_discount(12801, 28) is None)
    reply("919100000001", "haan 1 pcs")
    reply("919100000001", "1000")  # MRP
    a_posts = [b for c, b in group_posts if c == GA and "R21A01" in b]
    check("vendor with a Portal discount: asked pcs, MRP, then days -- not discount",
          len(a_posts) == 3 and "din" in a_posts[-1].lower() and not any("discount" in b.lower() for b in a_posts))
    reply("919100000002", "haan 1 pcs")
    reply("919100000002", "1000")
    b_posts =[b for c, b in group_posts if c == GB and "R21A01" in b]
    check("vendor with no discount on the Portal IS asked the discount", "discount" in b_posts[-1].lower())
    reply("919100000001", "2 din")
    reply("919100000002", "10")
    reply("919100000002", "2 din")
    settle()
    p1 = payload_for(12801) or {}
    check("...the Portal discount is his quote (15% beats 10% in the same band)",
          p1.get("supplier_dealer_id") == 27 and float(p1.get("quoted_discount_percent") or 0) == 15.0)

    # priority list: Beta first -> Beta wins the same band even with a smaller discount
    prio_file = _TMP / "prio.json"
    prio_file.write_text(json.dumps({"vendors": [vendors["B"]]}), encoding="utf-8")
    portal_settings.priority_file = str(prio_file)
    rounds._dealer_discounts.clear()
    clock["now"] = datetime(2026, 12, 1, 12, 5)
    cust(12802, "R21A02", disc="5.00", price="950.00")
    round_with_portal()
    settle(1)
    for number, disc in (("919100000001", "20"), ("919100000002", "12")):
        reply(number, "haan 1 pcs")
        reply(number, "1000")
        reply(number, disc)
        reply(number, "2 din")
    settle()
    check("priority list: Beta (listed) beats Alpha's bigger discount in the same band",
          (payload_for(12802) or {}).get("supplier_dealer_id") == 28)
    clock["now"] = datetime(2026, 12, 1, 15, 5)
    cust(12803, "R21A03", disc="5.00", price="950.00")
    round_with_portal()
    settle(1)
    reply("919100000001", "haan 1 pcs"); reply("919100000001", "1000"); reply("919100000001", "20"); reply("919100000001", "1 din")
    reply("919100000002", "haan 1 pcs"); reply("919100000002", "1000"); reply("919100000002", "12"); reply("919100000002", "5 din")
    settle()
    check("...but a faster band still beats priority (TAT first)",
          (payload_for(12803) or {}).get("supplier_dealer_id") == 27)
    portal_settings.priority_file = str(_TMP / "none.json")

    # 20 Nov: the brand-wise order from the dashboard's Vendors page (no file)
    def set_order(**pos):
        with get_session() as s:
            for code, prio in pos.items():
                row = s.execute(select(am.VendorBrand).where(am.VendorBrand.vendor_id == vendors[code],
                                                           am.VendorBrand.brand == "HYUNDAI")).scalar_one()
                row.priority = prio

    def race(sol, part, when, alpha_disc, beta_disc):
        clock["now"] = when
        cust(sol, part, disc="5.00", price="950.00")
        round_with_portal()
        settle(1)
        for number, disc in (("919100000001", alpha_disc), ("919100000002", beta_disc)):
            reply(number, "haan 1 pcs"); reply(number, "1000"); reply(number, disc); reply(number, "2 din")
        settle()
        return (payload_for(sol) or {}).get("supplier_dealer_id")

    set_order(B=1, A=2)
    check("dashboard order: Beta #1 beats Alpha's bigger discount in the same band",
          race(12820, "R21A20", datetime(2026, 12, 5, 9, 5), "20", "12") == 28)
    set_order(A=1, B=1)
    check("...vendors at the same position still go by the bigger discount",
          race(12821, "R21A21", datetime(2026, 12, 5, 12, 5), "20", "12") == 27)

    # vendor quotes below the customer's discount -> Prateek sir told at once
    before_p = len(to_prateek())
    clock["now"] = datetime(2026, 12, 2, 9, 5)
    cust(12804, "R21A04", disc="18.00", price="820.00")
    round_with_portal()
    settle(1)
    reply("919100000001", "haan 1 pcs"); reply("919100000001", "1000"); reply("919100000001", "12"); reply("919100000001", "2 din")
    alerts = [b for b in to_prateek()[before_p:] if "Kam discount" in b]
    check("low discount: Prateek sir told (12% vs customer's 18%)",
          len(alerts) == 1 and "R21A04" in alerts[0] and "12%" in alerts[0] and "18%" in alerts[0] and "Alpha Motors" in alerts[0])
    check("...and the vendor group never sees it", not any("Kam discount" in b or "customer" in b.lower()
                                                           for c, b in group_posts))

    # 6 hours, no answer: Prateek sir names a NEW vendor with no group -> saved, not asked one to one
    clock["now"] = datetime(2026, 12, 3, 9, 5)
    cust(12805, "R21A05")
    round_with_portal()
    settle(1)
    clock["now"] = datetime(2026, 12, 3, 15, 10)
    settle(1)
    ao5 = f"AO-{demand(12805).advance_order_id}"
    check("6 hours: escalated with the new-vendor option",
          any(ao5 in b and "naya vendor" in b and "R21A05" in b for b in to_prateek()))
    # 21 Nov 2026: with ONE escalation waiting, Prateek sir's vendor-approval
    # tap and his "Hi" were taken here ("samajh nahi aaya") and VR-27 was lost.
    with get_session() as s:
        claimed = {t: esc.handle_reply(PRATEEK, t, s) for t in ("VRBTN APPROVE 27", "APPROVE VR-27", "Approved", "Hi")}
    check("one escalation waiting: vendor-approval tap / APPROVE VR / 'Approved' / 'Hi' are NOT taken",
          not any(claimed.values()))
    sent_before = len(sent)
    with get_session() as s:
        check("'AO vendor <name> <mobile>' is understood", esc.handle_reply(PRATEEK, f"{ao5} vendor Naya Motors 9811122233", s))
    check("...the new vendor (no group) is NOT messaged one to one",
          not any(to == "919811122233" for to, _b in sent[sent_before:]))
    with get_session() as s:
        nv = s.execute(select(Vendor).where(Vendor.name == "Naya Motors")).scalar_one_or_none()
        on_list = nv is not None and s.execute(select(am.VendorBrand).where(
            am.VendorBrand.vendor_id == nv.id, am.VendorBrand.brand == "HYUNDAI")).scalar_one_or_none() is not None
    check("...but saved on the brand's vendor list", on_list)
    check("...and Prateek sir is told why", any("group abhi nahi" in b for b in to_prateek()))

    # several parts by CSV, discount set on the Portal for Alpha: the blank discount is not asked
    rounds._dealer_discounts.clear()
    clock["now"] = datetime(2026, 12, 4, 9, 5)
    cust(12810, "R21A10")
    cust(12811, "R21A11")
    for sol in (12810, 12811):
        portal.options[sol] = [{"dealer_id": 27, "discount_percent": "14.00"}, {"dealer_id": 28}]
    group_posts.clear()
    round_with_portal()
    settle(1)
    filled2 = _TMP / "filled2.csv"
    filled2.write_text("Part No,Qty Needed,Available Qty,Available (Yes/No),Discount %,Days (TAT)" + chr(10)
                       + "R21A10,1,1,Yes,," + chr(10) + "R21A11,1,1,Yes,," + chr(10), encoding="utf-8")
    before_posts = len(group_posts)
    with get_session() as s:
        took = engine.handle_enquiry_file("919100000001", filled2, "filled2.csv", s, clock["now"])
    new_posts = [b for c, b in group_posts[before_posts:] if c == GA]
    check("CSV + Portal discount: after the CSV only days are asked, not the discount",
          took and len(new_posts) == 1 and "din" in new_posts[0] and "discount" not in new_posts[0].lower())

    # evening report (rows carry the real clock's dates; put 12801-12803 on 25 Nov)
    with get_session() as s:
        for d in s.execute(select(m.PortalDemandLine).where(
                m.PortalDemandLine.source_order_line_id.in_([12801, 12802, 12803]))).scalars():
            d.first_seen_at = d.updated_at = datetime(2026, 12, 1, 9, 0)
            for a in s.execute(select(m.PortalAllocation).where(m.PortalAllocation.demand_line_id == d.id)).scalars():
                a.created_at = datetime(2026, 12, 1, 16, 0)
    with get_session() as s:
        data = reporting.collect(s, date(2026, 12, 1))
    text = reporting.report_text(data, excel=True)
    check("report: brand-wise and vendor-wise counts", data["totals"]["bought"] >= 3
          and "HYUNDAI" in text and "Alpha Motors" in text and "Brand wise" in text)
    check("report Excel builds", reporting.report_excel(data)[:2] == b"PK")
    before_p = len(to_prateek())
    with get_session() as s:
        check("report goes to Prateek sir", reporting.send_report(s, date(2026, 12, 1))
              and any("Purchase report" in b for b in to_prateek()[before_p:]))
    # ------------------------------------------------------------------ 22
    print("\n[22] live mode with a vendor allow-list (21 Nov 2026)")
    allow_file = _TMP / "pilot_live.json"
    allow_file.write_text(json.dumps({"allowed_vendors": [vendors["B"]], "skip_lines": [12903]}), encoding="utf-8")
    portal_settings.pilot_file = str(allow_file)
    portal_settings.mode = "live"
    cfg.ask_mode = "one"
    clock["now"] = datetime(2026, 12, 8, 9, 5)
    group_posts.clear()
    cust(12901, "R22H01")                 # HYUNDAI: Alpha and Beta sell it, only Beta allowed
    cust(12902, "R22T01", brand="TATA")   # TATA: only the Tata vendor, not allowed
    cust(12903, "R22H03")                 # a skip line
    tata_alerts = len(sent)
    round_with_portal()
    settle(1)
    check("live + allow-list: the Hyundai part goes to Beta's group only",
          posts(GB, "R22H01") == 1 and posts(GA, "R22H01") == 0)
    check("...a Tata part (no allowed vendor) is not taken and waits untouched",
          posts(GT, "R22T01") == 0 and demand(12902).state == m.DISCOVERED)
    check("...no 'no vendor for this brand' alert for it",
          not any("R22T01" in b for _t, b in sent[tata_alerts:]))
    check("...a skip line is never asked in live mode", demand(12903).state == m.DISCOVERED
          and posts(GA, "R22H03") == 0 and posts(GB, "R22H03") == 0)
    # a line that left the Portal (not seen for 2 days) is never sent
    clock["now"] = datetime(2026, 12, 9, 12, 5)
    group_posts.clear()
    cust(12904, "R22H04")                 # still on the Portal
    cust(12905, "R22H05")                 # will leave the Portal
    with get_session() as s:
        service.poll(s, client, clock["now"].date())
        s.commit()
    portal.rows.pop(12905, None)
    with get_session() as s:
        d = s.execute(select(m.PortalDemandLine).where(m.PortalDemandLine.source_order_line_id == 12905)).scalar_one()
        d.last_seen_at = clock["now"] - timedelta(days=2)
        s.commit()
    round_with_portal()
    settle(1)
    check("a line the Portal still shows is sent", posts(GB, "R22H04") == 1)
    check("a line not on the Portal for 2 days is NOT sent and waits",
          posts(GB, "R22H05") == 0 and posts(GA, "R22H05") == 0 and demand(12905).state == m.DISCOVERED)
    # Active Procurement is read again right before a question / reminder goes
    # out: a part that left it is not asked again
    clock["now"] = datetime(2026, 12, 10, 12, 5)
    group_posts.clear()
    for sol, part in ((12906, "R22H06"), (12907, "R22H07")):
        cust(sol, part)
        portal.rows[sol]["order_date"] = clock["now"].date().isoformat()
    with get_session() as s:
        service.poll(s, client, clock["now"].date())
        s.commit()
        slot = rounds.round_due(s, clock["now"])
        rounds.mark_round(s, slot, service.enquire(s, client))
        s.commit()
    check("re-check: both parts on the Portal are asked at the round",
          posts(GB, "R22H06") == 1 and posts(GB, "R22H07") == 1)
    portal.rows.pop(12907, None)          # someone took it off the Portal meanwhile
    clock["now"] = datetime(2026, 12, 10, 15, 10)   # reminder time, no poll ran in between
    settle(1)
    check("...the reminder still goes for the part on the Portal", posts(GB, "R22H06") == 2)
    check("...the part that left Active Procurement gets NO reminder and its line is closed",
          posts(GB, "R22H07") == 1 and demand(12907).state == m.CLOSED)
    allow_file.write_text(json.dumps({}), encoding="utf-8")
    cfg.ask_mode = "one"
    portal_settings.rounds = False

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
