"""Self-contained check of the purchase bot's AI layer (1 Oct 2026):
Gemini -> NVIDIA -> fixed rules, the vendor-reply reader, the staff
assistant, its memory, and the AI-down alarm.

Every model is replaced by a stand-in: nothing leaves this machine, nothing
is spent, nobody is messaged.

    venv\\Scripts\\python.exe -m backend.scripts.check_ai_layer
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="ai-layer-check-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ["WHATSAPP_ACCESS_TOKEN"] = ""
os.environ["WHATSAPP_ADMIN_PHONE_NUMBER"] = "919999900000"
os.environ["GEMINI_API_KEY"] = "test-gemini-key"
os.environ["NVIDIA_API_KEY"] = "test-nvidia-key"
os.environ["AI_DOWN_AFTER_FAILURES"] = "3"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def main() -> int:
    import backend.app.main  # noqa: F401 -- registers every table
    from backend.app.ai import llm
    from core.db import init_db

    init_db(force=True)

    print("\n[1] Gemini first, NVIDIA as backup, then nothing")
    check("JSON is found inside ``` fences and prose", llm.extract_json('Sure!\n```json\n{"tool": "help"}\n```') == {"tool": "help"})
    alarms: list[str] = []
    llm._alarm = lambda text, *, key, force=False: alarms.append(text)
    calls: list[str] = []

    def gemini_down(*_a):
        calls.append("gemini")
        raise RuntimeError("HTTP 429")

    def nvidia_ok(*_a):
        calls.append("nvidia")
        return '{"tool": "help"}'

    llm._CALLERS.update(gemini=gemini_down, nvidia=nvidia_ok)
    data, provider = llm.ask_json("s", "u")
    check("Gemini failing -> NVIDIA answers", data == {"tool": "help"} and provider == "nvidia" and calls == ["gemini", "nvidia"])
    llm.ask_json("s", "u")
    check("no alarm after 2 failures", not alarms)
    llm.ask_json("s", "u")
    check("the AI-down alarm fires on the 3rd failure in a row, naming the backup", len(alarms) == 1 and "gemini" in alarms[0] and "nvidia" in alarms[0])
    llm.ask_json("s", "u")
    check("...and does not repeat every message (once an hour)", len(alarms) == 1)
    llm._CALLERS.update(gemini=lambda *_a: '{"ok": true}')
    llm.ask_json("s", "u")
    check("Gemini back: 'AI wapas chal raha hai'", len(alarms) == 2 and "wapas" in alarms[1])
    llm._CALLERS.update(gemini=lambda *_a: None, nvidia=lambda *_a: "no json here")
    check("no provider answers sensibly -> (None, None), never an exception", llm.ask_json("s", "u") == (None, None))

    print("\n[2] reading a vendor's reply the fixed rules could not")
    from backend.app.advance_orders import ai_reply
    from backend.app.advance_orders.parser import parse_vendor_reply

    today = date(2026, 10, 1)
    one = [(1, "16510M68K10", 10)]
    two = [(1, "16510M68K10", 10), (2, "2630002752", 4)]
    text = "bhai 3 piece hai baaki next week"
    check("the fixed rules read it only partly (no delivery time)", (parse_vendor_reply(text, one, today) or {}).get(1) is None or parse_vendor_reply(text, one, today)[1].tat_days is None)

    def ai_says(payload):
        llm.ask_json = lambda *a, **k: (payload, "gemini")

    real_ask = llm.ask_json
    ai_says({"answers": [{"part": "16510M68K10", "available": True, "quantity": 3, "days": 7}]})
    r = ai_reply.read(text, one, today, want_rate=False)
    check("the AI reads '3 piece ... next week' as 3 pieces in 7 days", r and r[1].available_qty == 3 and r[1].tat_days == 7)
    ai_says({"answers": [{"part": "16510M68K10", "available": True, "quantity": 5}]})
    check("a quantity he never wrote (5) throws the whole answer away", ai_reply.read(text, one, today, want_rate=False) is None)
    ai_says({"answers": [{"part": "16510M68K10", "available": True, "days": 4}]})
    check("an invented delivery time (4 days) is thrown away", ai_reply.read(text, one, today, want_rate=False) is None)
    ai_says({"answers": [{"part": "9999XX", "available": True}]})
    check("a part we never asked about is thrown away", ai_reply.read(text, one, today, want_rate=False) is None)
    ai_says({"answers": [{"part": "16510M68K10", "available": True, "rate": 450}]})
    r = ai_reply.read("haan 450 wala bhi hai", one, today, want_rate=False)
    check("a rate is ignored when we did not ask him for one", r and r[1].quoted_rate is None)
    r = ai_reply.read("haan rate 450", one, today, want_rate=True)
    check("...and kept when we did, because 450 is in his words", r and r[1].quoted_rate == Decimal("450"))
    ai_says({"answers": [{"part": "16510M68K10", "available": True, "quantity": 10, "rate": 450, "days": 3}]})
    check("digits inside a part number never vouch for a quantity (16510M68K10 is not '10')", ai_reply.read("16510M68K10 ka rate 450 padega, 3 din", one, today, want_rate=True) is None)
    ai_says({"answers": [{"part": "2630002752", "available": True, "quantity": 2630002752}]})
    check("...nor does an all-digit part number", ai_reply.read("2630002752 hai", two, today, want_rate=False) is None)
    ai_says({"answers": [{"part": "16510M68K10", "available": True, "rate": 450, "days": 3}]})
    r = ai_reply.read("16510M68K10 ka rate 450 padega, 3 din", one, today, want_rate=True)
    check("...while the real figures in the same reply still count (450, 3 days)", r and r[1].quoted_rate == Decimal("450") and r[1].tat_days == 3)
    ai_says({"unclear": True, "answers": []})
    check("'unclear' -> nothing (the admin is told, as before)", ai_reply.read("sir call kar lo", two, today, want_rate=False) is None)
    ai_says({"answers": [{"part": "16510M68K10", "available": True, "days": 1}, {"part": "2630002752", "available": False}]})
    r = ai_reply.read("pehla wala kal tak, dusra nahi milega", two, today, want_rate=False)
    check("'kal' is allowed as 1 day; a refusal needs no number", r and r[1].tat_days == 1 and r[2].available is False)
    ai_says({"answers": [{"part": "16510M68K10", "available": True, "date": "2026-10-20"}]})
    r = ai_reply.read("20 tarikh tak aa jayega", one, today, want_rate=False)
    check("a date he named (the 20th) is used", r and r[1].eta == date(2026, 10, 20) and r[1].tat_days == 19)

    # Through the real reply handler: the fixed rules fail, the AI reads it.
    from backend.app.advance_orders import models as m
    from backend.app.advance_orders import service
    from backend.app.advance_orders.config import advance_order_settings as cfg
    from core.db import get_session
    from core.models import Vendor

    sent: list[tuple[str, str]] = []
    service._send_text = lambda to, body: sent.append((to, body)) or "wamid.x"
    service._internal_numbers = lambda s: ["919999900000"]
    cfg.vendor_hours, cfg.quote_fanout, cfg.vendor_template = "00:00-23:59", 1, ""
    with get_session() as s:
        v = Vendor(name="AI Vendor", vendor_code="AIV_CT")
        s.add(v)
        s.flush()
        s.add_all([m.VendorBrand(brand="FORD9", vendor_id=v.id, priority=1, discount_type=m.DISC_PERCENT, discount_pct=Decimal("11")),
                   m.AdvanceVendorContact(vendor_id=v.id, whatsapp_number="919500000001")])
        vid = v.id
    t = datetime(2026, 10, 1, 11, 0)
    service.now_ist_naive = lambda: t
    with get_session() as s:
        oid = service.create_order({"external_ref": "ADV-AI", "lines": [{"part_number": "FD-77", "brand": "FORD9", "qty": 10}]}, s).id
    reply = "stock me 3 hi bache hain, 2 din"
    fixed = parse_vendor_reply(reply, [(1, "FD-77", 10)], today)
    check("the fixed rules misread 'stock me 3 hi bache hain' as available with no limit", fixed and fixed[1].available and fixed[1].available_qty is None)
    ai_says({"answers": [{"part": "FD-77", "available": True, "quantity": 3, "days": 2}]})
    with get_session() as s:
        service.handle_vendor_text("919500000001", reply, s, t + timedelta(minutes=5))
    with get_session() as s:
        got = sorted((l.qty, l.status) for l in s.get(m.AdvanceOrder, oid).lines)
    check("end to end: the unread '3' brings the AI in, and its reading wins -- 3 found, 7 to find elsewhere", (3, m.LINE_AVAILABLE) in got and (7, m.LINE_UNAVAILABLE) in got)
    llm.ask_json = real_ask

    print("\n[3] the staff assistant")
    from backend.app.ai import staff_assistant as sa
    from backend.app.integrations.whatsapp.models import WhatsAppRegisteredNumber
    from backend.app.workers import document_worker as worker
    from backend.app.integrations.whatsapp.parser import IncomingWhatsAppText

    replies: list[tuple[str, str]] = []
    import backend.app.integrations.whatsapp.outbound as outbound

    outbound.send_reply_safe = lambda to, body: replies.append((to, body)) or True
    worker.send_reply_safe = outbound.send_reply_safe
    docs: list[tuple] = []
    outbound.send_document_safe = lambda to, content, name, mime, caption=None: docs.append((to, name, caption)) or True
    with get_session() as s:
        late = Vendor(name="Late Vendor", vendor_code="LTV_CT")
        s.add(late)
        s.flush()
        s.add(WhatsAppRegisteredNumber(whatsapp_number="919500000099", vendor_id=late.id))
    from core.services import inventory_import_service as imp

    stock = _TMP / "ai_stock.csv"
    stock.write_text("PartNo,Part Description,Qty\nFD-77,OIL FILTER,6\n", encoding="utf-8")
    with get_session() as s:
        imp.run_import(vid, stock, s)

    staff = "919999900000"
    check("an admin number is staff; a vendor's is not", sa.is_staff(staff) and not sa.is_staff("919500000001"))

    def staff_text(body, tool=None, args=None):
        replies.clear()
        if tool is not None:
            llm.ask_json = lambda *a, **k: ({"tool": tool, "args": args or {}}, "gemini")
        worker._handle_incoming_whatsapp_text(IncomingWhatsAppText(sender=staff, message_id="s", text=body))
        return [b for to, b in replies if to == staff]

    out = staff_text("aaj kisne stock nahi bheja?", "pending_vendors")
    check("'aaj kisne stock nahi bheja' -> the pending list, from the database", out and "Late Vendor" in out[-1])
    out = staff_text("FD77 kiske paas hai", "find_part", {"part": "FD77"})
    check("'FD77 kiske paas hai' -> who holds it and how many", out and "AI Vendor" in out[-1] and "6" in out[-1])
    out = staff_text("ai vendor ka stock bhejo", "vendor_stock", {"vendor": "ai vendor"})
    check("'... ka stock bhejo' -> his stock sent as an Excel file", docs and docs[-1][0] == staff and docs[-1][1].endswith(".xlsx"))
    out = staff_text("ADV-AI ka kya hua", "order_status", {"ref": "ADV-AI"})
    check("'ADV-AI ka kya hua' -> the order's lines and status", out and "FD-77" in out[-1])

    reminded: list[str] = []
    import backend.app.integrations.whatsapp.daily_stock as daily_stock

    daily_stock.handle_reminder_command = lambda sender: reminded.append(sender)
    out = staff_text("vendors ko reminder bhej do", "send_reminder")
    check("'reminder bhej do' ASKS first -- nothing sent yet", out and "haan" in out[-1] and not reminded)
    staff_text("haan")
    check("...'haan' sends it", reminded == [staff])
    staff_text("reminder bhej do", "send_reminder")
    staff_text("nahi")
    check("...'nahi' does not", reminded == [staff])

    out = staff_text("thanks", "none")
    check("a message that is not a request gets the usual instructions", out and "Vendor" in out[-1])
    llm.ask_json = lambda *a, **k: (None, None)
    out = staff_text("aaj kisne bheja?")
    check("no AI answering -> the usual instructions, nothing invented", out and "Vendor" in out[-1])
    out = staff_text("hi")
    check("'hi' starts afresh and shows what the assistant can do", out and "kiske paas" in out[-1])

    llm.ask_json = lambda *a, **k: ({"tool": "pending_vendors"}, "gemini")
    replies.clear()
    worker._handle_incoming_whatsapp_text(IncomingWhatsAppText(sender="919500000001", message_id="v", text="aaj kisne stock nahi bheja?"))
    check("a VENDOR cannot use the staff assistant", not any("Late Vendor" in b for _, b in replies))
    llm.ask_json = real_ask

    print("\n[4] photos: Gemini first")
    from backend.app.ai import vision

    llm.ask_json = lambda *a, **k: ({"rows": [{"part": "16510M68K10", "quantity": 5}, {"part": "2630002752", "quantity": None}]}, "gemini")
    vision.shrink = lambda b: b
    text, model = vision.read_stock_image(b"fake")
    check("Gemini's rows become PART QTY lines; an unread quantity becomes '?'", text == "16510M68K10 5\n2630002752 ?" and model.startswith("gemini"))
    llm.ask_json = real_ask

    print("\n" + ("ALL CHECKS PASSED" if failures == 0 else f"{failures} CHECK(S) FAILED"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
