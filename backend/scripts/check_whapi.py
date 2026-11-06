"""Check: the bot in WhatsApp groups through Whapi.Cloud (27 Oct 2026).

Throwaway SQLite, never the real database; every Whapi and Meta call is
captured, nothing is sent:

    .venv\\Scripts\\python.exe -m backend.scripts.check_whapi
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="whapi-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "WHAPI_ENABLED": "true",
    "WHAPI_TOKEN": "test-token",
    "WHAPI_WEBHOOK_SECRET": "s3cret",
    "WHAPI_TEAM_GROUP": "120363000000000001",
    "WHAPI_VENDOR_GROUPS": "CT_MSP=120363000000000002@g.us",
    "STOCK_FORMAT_TEAM_NUMBERS": "Seema:9289015770,Alam:9217030408,Rajkumar:9217396458",
    "WHATSAPP_ACCESS_TOKEN": "meta-token",
    "WHATSAPP_PHONE_NUMBER_ID": "1",
})
TEAM, VENDOR = "120363000000000001@g.us", "120363000000000002@g.us"

failures = 0


def check(name: str, cond: bool) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        failures += 1


def msg(chat, sender, text=None, doc=None, **extra):
    m = {"id": f"m{len(sender)}{hash((chat, sender, text, str(doc))) & 0xffff}", "chat_id": chat,
         "from": sender, "from_me": False, "source": "mobile", "timestamp": 1, "type": "text"}
    if text is not None:
        m["text"] = {"body": text}
    if doc is not None:
        m["type"] = "document"
        m["document"] = {"id": "media-1", "file_name": doc, "mime_type": "application/vnd.ms-excel"}
    m.update(extra)
    return m


class FakeResponse:
    def __init__(self, body=None, content=b""):
        self._body, self.content, self.status_code = body or {}, content, 200

    def json(self):
        return self._body


def main() -> int:
    from core.db import get_session, init_db
    from core.models import Vendor
    from backend.app.integrations.whatsapp import chat_log, registry, whapi
    from backend.app.integrations.whatsapp import client as client_mod
    from backend.app.integrations.whatsapp.client import WhatsAppClient
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    calls: list[tuple[str, str, dict]] = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json") or {}))
        if path.startswith("/media/"):
            return FakeResponse(content=b"xlsx-bytes")
        return FakeResponse({"sent": True, "message": {"id": f"out{len(calls)}"}})

    whapi._request = fake_request
    meta_sent: list[str] = []
    real_client = client_mod.httpx.Client
    client_mod.httpx.Client = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Meta must not be called"))
    chat_log.template_text = lambda name, params, client=None: "Hello Mohan, please share today's stock."

    init_db(force=True)
    with get_session() as s:
        mohan = Vendor(name="Mohan Spares Parts Pvt. Ltd.", vendor_code="CT_MSP")
        s.add(mohan)
        s.flush()
        registry.register_vendor_number("9711175797", mohan.id, s)

    print("\n[1] only the bot's groups are read")
    texts, files = whapi.parse({"messages": [
        msg("919999999999@s.whatsapp.net", "919999999999", "personal chat"),
        msg("120363999999999999@g.us", "919217396458", "bot hi"),
        msg(TEAM, "919217396458", "bot sent this", source="api"),
    ]})
    check("personal chats, other groups and our own sends ignored", not texts and not files)

    print("\n[2] team group: only messages meant for the bot")
    texts, files = whapi.parse({"messages": [
        msg(TEAM, "919217396458", "lunch kab hai?"),
        msg(TEAM, "919217396458", "bot Mohan ka stock bhejo"),
        msg(TEAM, "919217030408", "48400M69R00 available?"),
        msg(TEAM, "919217396458", doc="stock_Mohan.xlsx"),
    ]})
    check("chat between people ignored", "lunch kab hai?" not in [t.text for t in texts])
    check("'bot ...' passed on without the prefix", "Mohan ka stock bhejo" in [t.text for t in texts])
    check("a part number is a question for the bot", any("48400M69R00" in t.text for t in texts))
    check("file from Rajkumar, as Rajkumar", files and files[0].sender == "919217396458")
    check("file media id points at Whapi", files and files[0].media_id == "whapi:media-1")

    print("\n[3] vendor group speaks as the vendor")
    texts, files = whapi.parse({"messages": [
        msg(VENDOR, "919811111111", "35603M74T01 available hai"),
        msg(VENDOR, "919217396458", "ji bhej dijiye"),
        msg(VENDOR, "919822222222", doc="closing stock.xls"),
    ]})
    check("vendor's text as his registered number", [t.sender for t in texts] == ["919711175797"])
    check("our team's text in the vendor group ignored", "ji bhej dijiye" not in [t.text for t in texts])
    check("file in vendor group counts as the vendor's", files and files[0].sender == "919711175797")

    print("\n[4] replies go back to the group")
    check("vendor number -> vendor group", whapi.route_for("919711175797") == VENDOR)
    check("Rajkumar (wrote in team group) -> team group", whapi.route_for("919217396458") == TEAM)
    check("someone else -> normal Meta number", whapi.route_for("919000000000") is None)

    client = WhatsAppClient(whatsapp_settings)
    calls.clear()
    client.send_text_message("919711175797", "Do you have 48400M69R00?")
    client.send_text_message("919711175797", "Do you have 48400M69R00?")
    check("enquiry posted in vendor group once", [c[2].get("to") for c in calls] == [VENDOR])
    calls.clear()
    client.send_template_message("919711175797", "daily_stock_request", "en", ["Mohan"])
    check("template sent as plain text in the group", calls and calls[0][2]["body"].startswith("Hello Mohan"))

    print("\n[5] files and team alerts")
    from backend.app.integrations.whatsapp import outbound

    check("no 24 h window for a group", outbound.in_service_window("919711175797"))
    calls.clear()
    check("document to the team group", outbound.send_document_safe("919217396458", b"x", "a.xlsx", "application/x", "Stock"))
    check("document posted to the group", calls and calls[0][1] == "/messages/document" and calls[0][2]["to"] == TEAM)
    meta = client.get_media_url("whapi:media-1")
    check("group file downloaded from Whapi", client.download_media(meta["url"]) == b"xlsx-bytes")
    calls.clear()
    check("team alert posted to team group", whapi.post_team("⚠️ test alert") and calls[0][2]["to"] == TEAM)

    from backend.app.notifications import broker
    from backend.app.integrations.whatsapp import notification_forwarder as nf

    sent_direct: list[str] = []
    nf.send_reply_safe = lambda to, body: sent_direct.append(to) or True
    whatsapp_settings.forward_notifications = True
    whatsapp_settings.admin_phone_numbers = ["919999492550"]
    import threading
    real_thread = threading.Thread
    started: list = []
    threading.Thread = lambda target, daemon=True: started.append(target) or type("T", (), {"start": lambda self: target()})()
    calls.clear()
    nf.forward_notification(broker.Notification(id=1, level="error", title="Import failed", message="x"))
    check("general alert -> team group, not each person", len(calls) == 1 and not sent_direct)
    calls.clear()
    nf.forward_notification(broker.Notification(id=1, level="error", title="Portal", message="x", to=("919217030408",)))
    check("alert for Alam sir only still goes only to him", sent_direct == ["919217030408"] and not calls)
    threading.Thread = real_thread
    client_mod.httpx.Client = real_client

    print("\n[6] webhook needs the secret")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api.routes import whapi as route

    handled: list = []
    route.handle_incoming_whatsapp_text = lambda t: handled.append(t.text)
    route.handle_incoming_whatsapp_message = lambda m: handled.append(m.filename)
    app = FastAPI()
    app.include_router(route.router)
    tc = TestClient(app)
    body = {"messages": [msg(TEAM, "919217030408", "bot 17856M83B10 kiske paas hai")]}
    check("wrong secret refused", tc.post("/api/whapi/webhook", json=body, headers={"X-Whapi-Secret": "no"}).status_code == 403)
    r = tc.post("/api/whapi/webhook", json=body, headers={"X-Whapi-Secret": "s3cret"})
    check("right secret accepted and handed to the bot", r.status_code == 200 and handled == ["17856M83B10 kiske paas hai"])
    tc.post("/api/whapi/webhook", json=body, headers={"X-Whapi-Secret": "s3cret"})
    check("a re-delivered copy is not handled twice", handled == ["17856M83B10 kiske paas hai"])

    print("\n[7] shared number: the bot runs on Alam sir's own WhatsApp")
    os.environ.update({
        "WHAPI_SHARED_NUMBER": "true",
        "WHAPI_OWNER_NUMBER": "919217030408",
        "WHAPI_TEAM_MEMBERS": "Prateek sir:9999492550,NK Jain sir:9810238966",
    })
    client_mod.httpx.Client = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Meta must not be called"))
    calls.clear()
    whapi.send_text(TEAM, "Upload done: 120 parts")
    check("bot post carries the label", calls and calls[0][2]["body"].startswith("🤖 *Purchase Bot:*\nUpload done"))
    calls.clear()
    whapi.send_document(TEAM, b"y", "b.xlsx", "application/x", None)
    check("document caption is the label", calls and calls[0][2].get("caption") == "🤖 *Purchase Bot:*")
    os.environ["WHAPI_BOT_LABEL"] = "none"
    calls.clear()
    whapi.send_text(TEAM, "Upload done: 95 parts")
    check("WHAPI_BOT_LABEL=none -> no label at all", calls and calls[0][2]["body"] == "Upload done: 95 parts")
    os.environ.pop("WHAPI_BOT_LABEL")
    check("Alam sir (the owner) -> team group", whapi.route_for("919217030408") == TEAM)
    check("Prateek sir (team member) -> team group", whapi.route_for("919999492550") == TEAM)
    check("Ujjwal sir stays on the Meta number", whapi.route_for("918178412512") is None)
    calls.clear()
    client.send_text_message("919217030408", "✅ ADV-7 approved. Please arrange these parts.")
    client.send_text_message("919217396458", "✅ ADV-7 approved. Please arrange these parts.")
    check("same order to Alam and Rajkumar posted once", len(calls) == 1 and calls[0][2]["to"] == TEAM)
    texts, files = whapi.parse({"messages": [
        msg(TEAM, "919217030408", "@919217030408 aaj office aaoge?", from_me=True),
        msg(TEAM, "919217030408", "bot 48400M69R00 kiske paas hai", from_me=True),
        msg(TEAM, "919217030408", "auto reply", from_me=True, source="api"),
        msg(TEAM, "919999492550", "APPROVE VR-12"),
        msg(TEAM, "919217030408", doc="Ford stock.xls", from_me=True),
    ]})
    check("an @mention of Alam sir is not for the bot", not any("office" in t.text for t in texts))
    check("Alam sir's own 'bot ...' is handled as Alam sir",
          [(t.sender, t.text) for t in texts if "48400M69R00" in t.text] == [("919217030408", "48400M69R00 kiske paas hai")])
    check("the bot's own sends are never read back", not any("auto reply" in t.text for t in texts))
    check("'APPROVE VR-12' from Prateek sir reaches the bot", ("919999492550", "APPROVE VR-12") in [(t.sender, t.text) for t in texts])
    check("Alam sir's stock file counts as his", files and files[0].sender == "919217030408")

    print("\n[8] human wording and Hinglish")
    os.environ["WHAPI_BOT_LABEL"] = "none"
    calls.clear()
    whapi.send_text(TEAM, "✅ Stock received successfully. 1234 item(s) imported.")
    check("stock result said like a person", calls and calls[0][2]["body"] == "Stock update kar diya ✅ (1234 items)")
    check("no 'purchase bot' wording", "bot" not in whapi.humanize("Purchase bot apne fixed rules par chal raha hai.").lower())
    texts, _ = whapi.parse({"messages": [
        msg(TEAM, "919289015770", "Mohan ka stock aaya kya?"),
        msg(TEAM, "919289015770", "lunch kab karna hai"),
    ]})
    check("'Mohan ka stock aaya kya?' reaches the assistant without 'bot'", [t.text for t in texts] == ["Mohan ka stock aaya kya?"])
    with whapi.direct_only():
        check("a system alert never goes to the group", whapi.route_for("919999492550") is None)
    check("outside it, Prateek sir still -> team group", whapi.route_for("919999492550") == TEAM)

    print("\n[9] quiet vendor groups")
    ESS_DELHI, ESS_NOIDA = "120363000000000003@g.us", "120363000000000004@g.us"
    os.environ.update({
        "WHAPI_VENDOR_GROUP_MODE": "quiet",
        "WHAPI_VENDOR_GROUPS": f"CT_MSP={VENDOR},CT_EAA={ESS_DELHI}|{ESS_NOIDA}",
    })
    whapi._vendor_cache = None
    with get_session() as s:
        ess = Vendor(name="Ess Aay Automotive", vendor_code="CT_EAA")
        s.add(ess)
        s.flush()
        registry.register_vendor_number("9811000111", ess.id, s)
    check("both Ess Aay groups are read", {ESS_DELHI, ESS_NOIDA} <= whapi.allowed_groups())
    texts, files = whapi.parse({"messages": [
        msg(VENDOR, "919811111111", "bhai rate kam karo"),
        msg(VENDOR, "919811111111", doc="closing stock 29.xls"),
        msg(VENDOR, "919811111111", type="image", image={"id": "img-1"}),
        msg(VENDOR, "919289015770", doc="PO for Mohan.xlsx"),
        msg(VENDOR, "919217030408", doc="Alam ka PO.xlsx", from_me=True),
        msg(ESS_NOIDA, "919822222222", doc="ess aay noida.xlsx"),
    ]})
    check("vendor's chat is not answered", not texts)
    check("only the vendors' own stock files are taken",
          sorted((f.sender, f.filename) for f in files) == [("919711175797", "closing stock 29.xls"), ("919811000111", "ess aay noida.xlsx")])
    calls.clear()
    client.send_text_message("919711175797", "✅ Stock received successfully. 88 item(s) imported.")
    to_vendor = [c[2]["body"] for c in calls if c[2].get("to") == VENDOR]
    to_team = [c[2]["body"] for c in calls if c[2].get("to") == TEAM]
    check("vendor group gets only a short 'received'", to_vendor == ["Mil gaya ji, thank you 👍"])
    check("team group gets the result with the vendor's name",
          to_team == ["*Mohan Spares Parts Pvt. Ltd.* ki file:\nStock update kar diya ✅ (88 items)"])
    calls.clear()
    client.send_text_message("919711175797", "⚠️ Stock received. 80 item(s) imported, 8 row(s) skipped.")
    check("a second result in 10 minutes: no second 'received'", not [c for c in calls if c[2].get("to") == VENDOR])
    check("the vendor's direct chat is not moved to a group", whapi.route_for("919811000999") is None)

    print("\n[9b] vendor groups working like the official number")
    os.environ["WHAPI_VENDOR_GROUP_MODE"] = "same"
    whapi._vendor_cache = None
    texts, files = whapi.parse({"messages": [
        msg(VENDOR, "919711175797", "Part 123 qty 5"),
        msg(VENDOR, "919711175797", type="image", image={"id": "img-2"}),
        msg(VENDOR, "919811111111", "bhai rate kam karo"),
        msg(VENDOR, "919811111111", type="image", image={"id": "img-3"}),
        msg(VENDOR, "919811000111", "Ess Aay ka number, galat group"),
        msg(ESS_NOIDA, "919811000111", "Part 456 qty 2"),
        msg(VENDOR, "919289015770", "Seema ka message"),
        msg(VENDOR, "919217030408", "Alam ka message", from_me=True),
    ]})
    check("the vendor's own number: typed stock is read", sorted(t.text for t in texts) == ["Part 123 qty 5", "Part 456 qty 2"])
    check("the vendor's own number: photo is read", [f.is_photo for f in files] == [True])
    calls.clear()
    whapi._recent.clear()
    client.send_text_message("919711175797", "✅ Stock received successfully. 77 item(s) imported.")
    check("a file that came in fine gets just 'Mil gaya ji'",
          [c[2]["body"] for c in calls if c[2].get("to") == VENDOR] == ["Mil gaya ji, thank you 👍"])
    calls.clear()
    whapi._recent.clear()
    client.send_text_message("919711175797", "❌ We could not read this file. Please send an Excel file containing Part Number and Quantity columns.")
    check("a file that can't be read is explained in the group",
          [c[2]["body"] for c in calls if c[2].get("to") == VENDOR] == ["Ye file samajh nahi aa rahi. Excel mein Part No. aur Qty wali file bhej do please."])
    calls.clear()
    whapi._recent.clear()
    client.send_text_message("919711175797", "Is this PDF your stock list or an invoice?")
    check("a question from the bot reaches the group",
          [c[2]["body"] for c in calls if c[2].get("to") == VENDOR] == ["Is this PDF your stock list or an invoice?"])
    check("nothing goes to the team group", not [c for c in calls if c[2].get("to") == TEAM])
    calls.clear()
    client.send_text_message("919711175797", "Kuch parts CarTrends portal par nahi ja paaye.")
    check("anything about the Portal stays out of the group", not calls)
    os.environ["WHAPI_VENDOR_GROUP_MODE"] = "quiet"
    whapi._vendor_cache = None

    print("\n[10] groups saved in the database")
    from backend.app.integrations.whatsapp.models import WhapiVendorGroup

    NORTHEND = "120363000000000005@g.us"
    with get_session() as s:
        nd = Vendor(name="Northend Distributors", vendor_code="CT_ND")
        s.add(nd)
        s.flush()
        registry.register_vendor_number("7290096792", nd.id, s)
        s.add(WhapiVendorGroup(group_id=NORTHEND, vendor_code="CT_ND", name="CarTrends 🤝 Northend"))
    whapi.forget_cached_groups()
    check("a group from the table is a vendor group", whapi.vendor_group_codes().get(NORTHEND) == "CT_ND")
    check("groups from the settings still count", whapi.vendor_group_codes().get(VENDOR) == "CT_MSP")

    print("\n[11] 9:30 / 6 pm 'stock share kar dijiye'")
    from backend.app.integrations.whatsapp import group_stock_request as gsr
    from core.models import InventoryImport
    from core.time_utils import now_ist_naive

    os.environ["WHAPI_STOCK_REQUEST_ENABLED"] = "true"
    with get_session() as s:
        s.add(InventoryImport(vendor_id=nd.id, file_name="nd.xlsx", file_size_bytes=1, content_hash="nd-today",
                              status="COMPLETED", row_count=1, created_at=now_ist_naive()))
    calls.clear()
    pauses: list = []
    result = gsr.send_requests(sleep=pauses.append)
    posted = {c[2]["to"]: c[2]["body"] for c in calls if c[1] == "/messages/text"}
    check("every vendor group asked except the one that just sent stock", set(posted) == {VENDOR, ESS_DELHI, ESS_NOIDA})
    check("Northend (sent stock just now) is skipped", result["skipped"] == [NORTHEND])
    check("asked in plain words, no label", all("stock" in b.lower() and "bot" not in b.lower() for b in posted.values()))
    check("never forwarded to the team group", TEAM not in posted)
    check("a pause between groups", len(pauses) == 2)

    print("\n[12] a new vendor gets its own group")
    from backend.app.integrations.whatsapp import vendor_group

    os.environ["WHAPI_AUTO_VENDOR_GROUP"] = "true"
    created_calls: list = []

    def group_request(method, path, **kwargs):
        created_calls.append((method, path, kwargs.get("json") or {}))
        if method == "POST" and path == "/groups":
            return FakeResponse({"group_id": "120363000000000009@g.us", "participants": [
                {"id": "919217030408@s.whatsapp.net"}, {"id": "919289015770@s.whatsapp.net"}, {"id": "919217396458@s.whatsapp.net"}]})
        if path.endswith("/invite"):
            return FakeResponse({"invite_code": "AbCd123"})
        return FakeResponse({"message": {"id": "x"}})

    whapi._request = group_request
    invites: list = []
    import backend.app.integrations.whatsapp.outbound as ob
    real_reply = ob.send_reply_safe
    ob.send_reply_safe = lambda to, body: invites.append((to, body)) or True
    new_group = vendor_group.create_for_vendor("CT_NEW", "New   Vendor Spares", "9876500000")
    create = created_calls[0][2]
    check("group made with the vendor and the team",
          create.get("subject") == "CarTrends 🤝 New Vendor Spares" and create.get("participants") == ["919876500000", "919289015770", "919217396458"])
    check("saved as CT_NEW's group", whapi.vendor_group_codes().get(new_group) == "CT_NEW")
    check("welcome posted in it", any(c[1] == "/messages/text" and "swagat" in c[2].get("body", "") for c in created_calls))
    check("vendor not added (privacy) -> gets the invite link", invites and "chat.whatsapp.com/AbCd123" in invites[0][1])
    created_calls.clear()
    check("a vendor who already has a group gets no second one", vendor_group.create_for_vendor("CT_NEW", "New Vendor Spares", "9876500000") is None and not created_calls)
    ob.send_reply_safe = real_reply

    print("\n[13] team group unreachable -> straight to the person")
    meta_posts: list[dict] = []

    class MetaResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"messages": [{"id": "wamid.ok"}]}

    class MetaClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            meta_posts.append(json)
            return MetaResponse()

    def trial_over(method, path, **kwargs):
        if (kwargs.get("json") or {}).get("to") == TEAM:
            raise whapi.WhapiError('Whapi POST /messages/text: HTTP 402 {"error":{"code":402,"message":"trial version limit exceeded"}}')
        return FakeResponse({"message": {"id": "x"}})

    whapi._request = trial_over
    client_mod.httpx.Client = MetaClient
    ob.template_approved = lambda name: True
    check("before: Prateek sir's messages go to the team group", whapi.route_for("919999492550") == TEAM)
    ob.send_reply_safe("919999492550", "Namaste Prateek sir,\nNew vendor VR-15 NIRMAL OIL STORE. Reply APPROVE VR-15")
    check("the approval reached Prateek sir on the main number", [p.get("to") for p in meta_posts] == ["919999492550"])
    check("as the approved template (he has not written in 24 h)", meta_posts and meta_posts[0].get("type") == "template")
    check("the team group is now skipped for 10 minutes", whapi.route_for("919810238966") is None)
    meta_posts.clear()
    ob.send_reply_safe("919810238966", "Namaste NK Jain sir,\nNew vendor VR-15. Reply APPROVE VR-15")
    check("Jain sir's goes direct at once, no second try at the group", [p.get("to") for p in meta_posts] == ["919810238966"])
    calls.clear()
    meta_posts.clear()
    whapi._request = fake_request
    client.send_text_message("919711175797", "⚠️ Stock received. 8 item(s) imported, 1 row(s) skipped.")
    check("vendor groups are unaffected (report still posted for the team)",
          any(c[2].get("to") == TEAM and "Mohan" in c[2].get("body", "") for c in calls) and not meta_posts)
    whapi._team_down_at = 0.0
    client_mod.httpx.Client = real_client

    print("\n[14] answering a form in the team group")
    from backend.app.vendor_onboarding import models as vm

    with get_session() as s:
        s.add(vm.VendorRegistrationRequest(whatsapp_number="919289015770", current_step="brands_supplied",
                                           answers={"_on_behalf": True, "gstin": "07AAAPJ1384J1ZS", "vendor_name": "NIRMAL OIL STORE"}))
    texts, _ = whapi.parse({"messages": [
        msg(TEAM, "919289015770", "shell"),
        msg(TEAM, "919289015770", "Liqui Moly"),
        msg(TEAM, "919217396458", "shell"),
    ]})
    got = [(t.sender, t.text) for t in texts]
    check("Seema ji's plain answers reach her open form", got[:2] == [("919289015770", "shell"), ("919289015770", "Liqui Moly")])
    check("the same word from someone without a form is still ignored", ("919217396458", "shell") not in got)

    print("\n[16] internal documents never go into the group")
    from backend.app.integrations.whatsapp import vendor_purchase_output as vpo

    vpo.group_by_vendor = lambda reports, labels: {"Mohan": {"qty": 1, "lines": []}}
    vpo.collect_shortages = lambda reports, labels: []
    vpo.recipients_service.internal_file_recipients = lambda *a, **k: ["919999492550"]
    direct_seen: list = []
    vpo._send_vendor_workbook = lambda *a, **k: direct_seen.append(whapi._direct.get()) or 1
    vpo._send_vendor_texts = lambda *a, **k: direct_seen.append(whapi._direct.get()) or 1
    vpo.send_vendor_messages([], {})
    check("the purchase workbook for the team is sent direct, not to the group", direct_seen == [True])
    check("Prateek sir (team group member) -> would go direct inside it",
          (lambda: (lambda t: (whapi.route_for("919999492550"), whapi._direct.reset(t))[0])(whapi._direct.set(True)))() is None)

    print("\n[15] every group action lands in the live feed")
    from sqlalchemy import select

    from backend.app.integrations.whatsapp.models import WhapiActivity

    with get_session() as s:
        kinds = [k for (k,) in s.execute(select(WhapiActivity.kind))]
    check("files and messages taken are recorded", "received file" in kinds and "received" in kinds)
    check("replies, 'received' acks and stock requests are recorded", {"posted", "acknowledged", "stock request"} <= set(kinds))
    check("a created vendor group is recorded", "group created" in kinds)

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
