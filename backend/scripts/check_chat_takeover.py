"""Check: human takeover of a purchase-bot chat (28 Oct 2026).

Throwaway SQLite, never the real database. WhatsApp is a fake that records
every send (and can refuse one); nothing leaves this machine:

    .venv\\Scripts\\python.exe -m backend.scripts.check_chat_takeover
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="takeover-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'check.db').as_posix()}"
os.environ.update({
    "WHATSAPP_ACCESS_TOKEN": "meta-token",
    "WHATSAPP_PHONE_NUMBER_ID": "1",
    "WHAPI_ENABLED": "false",
})
VENDOR, OTHER = "919811111111", "919822222222"

failures = 0


def check(name: str, cond: bool, detail=None) -> None:
    global failures
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond or detail is None else f"\n       {detail!r}"[:400]))
    if not cond:
        failures += 1


class User:
    username = "ronak"
    full_name = "Ronak"


def main() -> int:
    from fastapi import HTTPException
    from sqlalchemy import select

    from core.db import get_session, init_db
    from backend.app.api.routes import chats as route
    from backend.app.integrations.whatsapp import client as client_mod, takeover
    from backend.app.integrations.whatsapp.models import AiConversationMessage, WhatsAppChatMessage
    from backend.app.integrations.whatsapp.outbound import send_reply_safe
    from backend.app.integrations.whatsapp.parser import IncomingWhatsAppText
    from backend.app.workers import document_worker

    # ---- WhatsApp: records every send; `refuse` makes the next one fail.
    sent: list[tuple[str, str]] = []
    refuse = {"next": False}

    class FakeResponse:
        def __init__(self, n):
            self._n = n

        def raise_for_status(self):
            return None

        def json(self):
            return {"messages": [{"id": f"wamid.out{self._n}"}]}

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None, **k):
            if refuse["next"]:
                refuse["next"] = False
                raise RuntimeError("(#131047) Re-engagement message")
            body = ((json or {}).get("text") or {}).get("body") or (json or {}).get("type")
            sent.append(((json or {}).get("to"), body))
            return FakeResponse(len(sent))

    client_mod.httpx.Client = FakeHttp
    client_mod._RETRY_BACKOFF_SECONDS = ()

    # ---- the bot: what its text handling does that matters here -- it reads
    # the AI memory and answers once.
    handled: list[tuple[str, str, str]] = []

    def bot(message):
        from backend.app.ai.staff_assistant import _recent, _remember

        memory = "\n".join(f"{r}: {t}" for r, t, _ in _recent(message.sender))
        handled.append((message.sender, message.text, memory))
        _remember(message.sender, "in", message.text)
        send_reply_safe(message.sender, f"Bot: answering {message.text!r}")

    document_worker._handle_incoming_whatsapp_text = bot
    init_db(force=True)

    seq = {"n": 0}

    def text(number, body):
        seq["n"] += 1
        document_worker.handle_incoming_whatsapp_text(IncomingWhatsAppText(sender=number, message_id=f"wamid.in{seq['n']}", text=body))

    def to(number):
        return [b for n, b in sent if n == number]

    def reset():
        sent.clear()
        handled.clear()

    def version(number):
        return takeover.view(number)["version"]

    def memory(number):
        with get_session() as s:
            return [(r.role, r.text) for r in s.execute(
                select(AiConversationMessage).where(AiConversationMessage.whatsapp_number == number).order_by(AiConversationMessage.id)
            ).scalars()]

    print("\n[1] not taken over: the bot answers as before")
    text(VENDOR, "48400M69R00 hai?")
    check("the bot handled it", len(handled) == 1)
    check("its reply went out", to(VENDOR) == ["Bot: answering '48400M69R00 hai?'"], sent)

    print("\n[2] take over: the bot goes quiet on THIS chat only")
    reset()
    r = route.switch_takeover(VENDOR, route.TakeoverIn(on=True, version=version(VENDOR)), User())
    check("switched on, by the logged-in user", r["ok"] and r["state"]["on"] and r["state"]["by"] == "Ronak", r)
    text(VENDOR, "Bhai Swift ka brake pad chahiye")
    text(VENDOR, "Right side wala")
    text(OTHER, "17220M68K00 available?")
    check("its messages did not reach the bot", [h[0] for h in handled] == [OTHER], handled)
    check("both kept as unanswered", [p["text"] for p in takeover.view(VENDOR)["pending"]] == ["Bhai Swift ka brake pad chahiye", "Right side wala"])
    check("and in the AI's memory", ("in", "Right side wala") in memory(VENDOR))
    check("the other chat was answered", len(to(OTHER)) == 1)

    print("\n[3] the bot's own messages to that chat are held back, not lost")
    reset()
    send_reply_safe(VENDOR, "✓ Stock imported: 1302 rows")
    check("nothing went out", to(VENDOR) == [], sent)
    held = takeover.view(VENDOR)["held"]
    check("kept for the admin", len(held) == 1 and held[0]["text"] == "✓ Stock imported: 1302 rows" and held[0]["sendable"], held)
    check("not shown as sent in the chat log", not _logged(get_session, VENDOR, "✓ Stock imported"))

    print("\n[4] the admin replies by hand")
    reset()
    r = route.send_by_hand(VENDOR, route.AdminTextIn(text="Ji, check karke batata hoon"), User())
    check("sent through WhatsApp", r["sent"] and to(VENDOR) == ["Ji, check karke batata hoon"], sent)
    check("it answered what was pending", takeover.view(VENDOR)["pending"] == [])
    check("logged as Ronak's, not the bot's", _logged(get_session, VENDOR, "Ji, check karke", sent_by="Ronak"))
    check("in the AI memory as a person at our desk", any(role == "out" and "Ronak" in t and "check karke" in t for role, t in memory(VENDOR)))

    print("\n[5] a reply WhatsApp refused leaves the question unanswered")
    text(VENDOR, "Price kitna hai?")
    refuse["next"] = True
    try:
        route.send_by_hand(VENDOR, route.AdminTextIn(text="₹1,800"), User())
        refused = False
    except HTTPException as exc:
        refused = exc.status_code == 502 and "24 hours" in str(exc.detail)
    check("refused, with the 24-hour reason", refused)
    check("still pending", [p["text"] for p in takeover.view(VENDOR)["pending"]] == ["Price kitna hai?"])

    print("\n[6] a held-back bot message the admin lets through")
    reset()
    hid = takeover.view(VENDOR)["held"][0]["id"]
    r = route.held_message(VENDOR, hid, route.HeldIn(action="send"), User())
    check("sent", r["ok"] and to(VENDOR) == ["✓ Stock imported: 1302 rows"])
    check("it does not answer the open question", len(takeover.view(VENDOR)["pending"]) == 1)
    check("gone from the list", takeover.view(VENDOR)["held"] == [])
    try:
        route.held_message(VENDOR, hid, route.HeldIn(action="send"), User())
        again = False
    except HTTPException as exc:
        again = exc.status_code == 404
    check("cannot be sent twice", again)

    print("\n[7] an admin switch made on an old view is refused")
    try:
        route.switch_takeover(VENDOR, route.TakeoverIn(on=False, version=version(VENDOR) - 1), User())
        conflict = False
    except HTTPException as exc:
        conflict = exc.status_code == 409
    check("409, still taken over", conflict and takeover.active(VENDOR))

    print("\n[8] hand back: the unanswered question is answered once, with context")
    reset()
    r = route.switch_takeover(VENDOR, route.TakeoverIn(on=False, version=version(VENDOR)), User())
    check("result: answered", r["result"] == "answered", r)
    check("the bot ran once, on the latest message", [h[1] for h in handled] == ["Price kitna hai?"], handled)
    check("its memory had the admin's reply and the earlier messages", handled and "check karke" in handled[0][2] and "Right side wala" in handled[0][2], handled and handled[0][2])
    check("the question it answers is not in memory twice", [t for role, t in memory(VENDOR) if t == "Price kitna hai?"].__len__() == 1, memory(VENDOR))
    check("one reply went out", len(to(VENDOR)) == 1)
    check("nothing pending", takeover.view(VENDOR)["pending"] == [])
    r = route.switch_takeover(VENDOR, route.TakeoverIn(on=False, version=version(VENDOR)), User())
    check("a second hand-back does nothing", r["result"] == "already with the bot" and len(to(VENDOR)) == 1)

    print("\n[9] hand back after the admin answered: silence")
    reset()
    route.switch_takeover(VENDOR, route.TakeoverIn(on=True, version=version(VENDOR)), User())
    text(VENDOR, "Delivery kab?")
    route.send_by_hand(VENDOR, route.AdminTextIn(text="Kal tak aa jayega"), User())
    reset()
    r = route.switch_takeover(VENDOR, route.TakeoverIn(on=False, version=version(VENDOR)), User())
    check("nothing pending, nothing sent", r["result"] == "nothing pending" and not handled and not sent, r)

    print("\n[10] the last message is only thanks: silence")
    for thanks in ("Thank you", "Ok ji", "Theek hai, thank you", "👍", "Shukriya bhai"):
        reset()
        route.switch_takeover(VENDOR, route.TakeoverIn(on=True, version=version(VENDOR)), User())
        text(VENDOR, "Rate bata do")
        text(VENDOR, thanks)
        r = route.switch_takeover(VENDOR, route.TakeoverIn(on=False, version=version(VENDOR)), User())
        check(f'"{thanks}" gets no reply', r["result"] == "acknowledgment" and not sent, r)

    print("\n[11] thanks WITH a question is answered")
    reset()
    route.switch_takeover(VENDOR, route.TakeoverIn(on=True, version=version(VENDOR)), User())
    text(VENDOR, "Thank you, payment kab aayega?")
    r = route.switch_takeover(VENDOR, route.TakeoverIn(on=False, version=version(VENDOR)), User())
    check("answered", r["result"] == "answered" and len(to(VENDOR)) == 1, r)

    print("\n[12] taken over again while the bot was answering: its reply is held")
    reset()
    route.switch_takeover(VENDOR, route.TakeoverIn(on=True, version=version(VENDOR)), User())
    text(VENDOR, "Order confirm?")
    real = document_worker._handle_incoming_whatsapp_text

    def slow_bot(message):
        takeover.set_mode(VENDOR, True, by="Alam")  # someone takes it back mid-answer
        real(message)

    document_worker._handle_incoming_whatsapp_text = slow_bot
    r = route.switch_takeover(VENDOR, route.TakeoverIn(on=False, version=version(VENDOR)), User())
    document_worker._handle_incoming_whatsapp_text = real
    check("result: still taken over", r["result"] == "still taken over", r)
    check("nothing went out", not sent, sent)
    check("the reply waits in the held list", any("Order confirm" in h["text"] for h in takeover.view(VENDOR)["held"]))
    check("the question is still pending for the admin", len(takeover.view(VENDOR)["pending"]) == 1)

    print("\n[13] a restart: the takeover and what is pending survive")
    with get_session() as s:
        row = takeover._row(s, VENDOR)
        row.resume = {"status": "processing"}
    takeover.on_boot()
    v = takeover.view(VENDOR)
    check("still taken over, still pending", v["on"] and len(v["pending"]) == 1)
    check("the cut-off hand-back is marked interrupted, not re-run", v["resume"]["status"] == "interrupted")

    print("\n[14] the Chats page sees it")
    page = route.get_chat(VENDOR, limit=300, before_id=None, db=_db())
    check("the chat carries the takeover state", page["takeover"]["on"] and page["takeover"]["by"] == "Alam")
    check("hand-typed messages carry the name", any(m.get("sent_by") == "Ronak" for m in page["messages"]))
    listing = route.list_chats(q="", limit=50, db=_db()) if "limit" in route.list_chats.__code__.co_varnames else route.list_chats(q="", db=_db())
    mine = next((c for c in listing if c["number"] == VENDOR), {})
    check("the chat list marks it", (mine.get("takeover") or {}).get("on") is True, mine)

    print("\n[15] audit trail")
    with get_session() as s:
        audit = takeover._row(s, VENDOR).audit or []
    events = {a["event"] for a in audit}
    check("on, off, reply, held sent and hand-back results are logged with who", {"takeover on", "takeover off", "admin reply", "held sent", "handover"} <= events and all(a.get("by") for a in audit), events)

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


def _db():
    from core.db import SessionLocal

    return SessionLocal()


def _logged(get_session, number, starts, sent_by=None):
    from sqlalchemy import select

    from backend.app.integrations.whatsapp.models import WhatsAppChatMessage

    with get_session() as s:
        rows = s.execute(select(WhatsAppChatMessage).where(WhatsAppChatMessage.whatsapp_number == number, WhatsAppChatMessage.direction == "out")).scalars().all()
        return any((r.text or "").startswith(starts) and (sent_by is None or r.sent_by == sent_by) for r in rows)


if __name__ == "__main__":
    sys.exit(main())
