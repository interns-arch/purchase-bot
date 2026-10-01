"""The purchase bot's assistant for staff (admin numbers and the purchase
team) -- the purchase-side twin of the sales bot's staff agent.

Staff write the way they talk ("aaj kisne stock nahi bheja?", "Ess Aay ka
stock bhejo", "16510M68K10 kiske paas hai"). The AI does ONE thing: decide
which of the tools below the message is asking for, and pull out its
argument. The tool then answers from the DATABASE, in fixed wording -- the
AI never states a number, a name or a status itself (AutoFlow's rule: a
reply with a number the tools never gave is not sent; here the AI never
writes the reply at all).

Anything that MESSAGES VENDORS (send reminder) is asked back first --
"12 vendors ko reminder bhejun? haan / nahi" -- because a misread request
must never reach a vendor.

It runs only for staff, and only after every existing command has had its
chance (register, send reminder, held files waiting for a name, ...), so
nothing that worked before changes. When no AI answers, staff get the old
instruction message.

Memory: the last few messages per staff number (`AiConversationMessage`),
so "uska bhi bhejo" can be read in context. "hi" starts afresh.
"""

from __future__ import annotations

import io
import os
import re
from datetime import timedelta

from sqlalchemy import select

from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

ENABLED = os.environ.get("AI_STAFF_ASSISTANT_ENABLED", "true").strip().lower() == "true"
MEMORY_KEEP = 20
MEMORY_SHOWN = 6
CONFIRM_MINUTES = 10

TOOLS = {
    "pending_vendors": "vendors who have NOT sent stock today",
    "received_vendors": "vendors who HAVE sent stock today",
    "find_part": "who holds a part and how many (args: part)",
    "vendor_stock": "send a vendor's current stock as an Excel file (args: vendor)",
    "order_status": "status of an advance order or dealer-stock order (args: ref, e.g. 12 or DS-12 or ADV-...)",
    "send_reminder": "send the stock reminder to vendors who have not sent stock today",
    "help": "what the assistant can do",
    "none": "the message is not a request for any of these",
}

SYSTEM = (
    "You route WhatsApp messages from Cartrends' purchase team to a tool. The team writes "
    "Hinglish, short forms and typos. Reply with JSON only: "
    '{"tool": "<name>", "args": {}}\n'
    "Tools:\n"
    + "\n".join(f"- {name}: {what}" for name, what in TOOLS.items())
    + "\nRules: choose exactly one tool. Copy a part number or vendor name exactly as written. "
    "Use the recent conversation to resolve words like 'uska', 'wahi', 'unka'. "
    "Examples: 'aaj kisne stock nahi bheja' -> pending_vendors; 'kis kis ka stock aa gaya' -> "
    "received_vendors; '16510M68K10 kiske paas hai' -> find_part {part: '16510M68K10'}; "
    "'ess aay ka stock bhejo' -> vendor_stock {vendor: 'ess aay'}; 'DS-12 ka kya hua' -> "
    "order_status {ref: 'DS-12'}; 'reminder bhej do' -> send_reminder; 'thanks' -> none."
)

HELP = (
    "Main ye kar sakta hoon — jaise bhi likhiye:\n"
    "• aaj kisne stock nahi bheja / kisne bheja\n"
    "• <part number> kiske paas hai\n"
    "• <vendor> ka stock bhejo (Excel)\n"
    "• <order no.> ka kya hua (advance / DS order)\n"
    "• reminder bhej do (pehle poochunga)\n"
    "Files ke liye: vendor / customer / invoice likh kar file bhejiye."
)

_YES = re.compile(r"^\s*(haan+|han|ha|haa|yes|y|ok|okay|theek|thik|kar do|bhej do|confirm)\s*[.!]*\s*$", re.I)
_NO = re.compile(r"^\s*(nahi+|nhi|no|n|mat|cancel|rehne do)\s*[.!]*\s*$", re.I)
_GREETING = re.compile(r"^\s*(hi+|hello|hey|namaste)\s*[.!]*\s*$", re.I)


# ------------------------------------------------------------------ memory
def _remember(number: str, role: str, text: str) -> None:
    from backend.app.integrations.whatsapp.models import AiConversationMessage
    from core.db import get_session

    with get_session() as session:
        session.add(AiConversationMessage(whatsapp_number=number, role=role, text=text[:2000], created_at=now_ist_naive()))
        session.flush()
        rows = session.execute(
            select(AiConversationMessage.id)
            .where(AiConversationMessage.whatsapp_number == number)
            .order_by(AiConversationMessage.id.desc())
            .offset(MEMORY_KEEP)
        ).scalars().all()
        for row_id in rows:
            session.delete(session.get(AiConversationMessage, row_id))


def _recent(number: str) -> list[tuple[str, str, object]]:
    from backend.app.integrations.whatsapp.models import AiConversationMessage
    from core.db import get_session

    with get_session() as session:
        rows = session.execute(
            select(AiConversationMessage)
            .where(AiConversationMessage.whatsapp_number == number)
            .order_by(AiConversationMessage.id.desc())
            .limit(MEMORY_SHOWN)
        ).scalars().all()
        return [(r.role, r.text, r.created_at) for r in reversed(rows)]


def _forget(number: str) -> None:
    from backend.app.integrations.whatsapp.models import AiConversationMessage
    from core.db import get_session

    with get_session() as session:
        for row in session.execute(select(AiConversationMessage).where(AiConversationMessage.whatsapp_number == number)).scalars():
            session.delete(row)


# ------------------------------------------------------------------ the tools
def _pending_vendors() -> str:
    from backend.app.integrations.whatsapp.daily_stock import participation_today
    from core.db import get_session

    with get_session() as session:
        received, pending, _ = participation_today(session)
    if not pending:
        return f"🎉 Sabne aaj stock bhej diya ({len(received)} vendors)."
    return f"⏳ Aaj {len(pending)} vendors ka stock baaki hai:\n" + "\n".join(f"• {name}" for name in pending)


def _received_vendors() -> str:
    from backend.app.integrations.whatsapp.daily_stock import participation_today
    from core.db import get_session

    with get_session() as session:
        received, pending, _ = participation_today(session)
    if not received:
        return "Aaj abhi tak kisi registered vendor ne stock nahi bheja."
    return f"✅ Aaj {len(received)} vendors ne stock bheja:\n" + "\n".join(f"• {name}" for name in received)


def _find_part(part: str) -> str:
    from core.db import get_session
    from core.ingestion.column_detector import normalise_part_number
    from core.models import InventoryImport, Vendor, VendorInventory
    from core.services.vendor_selection_service import _matchable_part_numbers

    key = normalise_part_number(part)
    if not key:
        return "Part number samajh nahi aaya. Jaise likhiye: 16510M68K10 kiske paas hai"
    with get_session() as session:
        numbers = _matchable_part_numbers(key, session)
        rows = session.execute(
            select(Vendor.name, VendorInventory.quantity_available, InventoryImport.created_at)
            .join(InventoryImport, InventoryImport.id == VendorInventory.import_id)
            .join(Vendor, Vendor.id == VendorInventory.vendor_id)
            .where(InventoryImport.is_active.is_(True), VendorInventory.normalized_part_number.in_(numbers), VendorInventory.quantity_available > 0)
            .order_by(VendorInventory.quantity_available.desc())
            .limit(12)
        ).all()
    if not rows:
        return f"{part}: kisi vendor ke current stock mein nahi hai."
    lines = [f"• {name} — {int(qty) if qty == int(qty) else qty} ({at.strftime('%d %b') if at else '?'})" for name, qty, at in rows]
    return f"📦 {part} inke paas hai:\n" + "\n".join(lines)


def _find_vendor(name: str):
    from core.db import get_session
    from core.models import Vendor
    from core.services import vendor_service

    with get_session() as session:
        vendor = vendor_service.get_vendor_by_name(name, session)
        if vendor is None:
            wanted = re.sub(r"[^a-z0-9]", "", name.lower())
            for candidate in session.execute(select(Vendor).where(Vendor.active.is_(True))).scalars():
                if wanted and wanted in re.sub(r"[^a-z0-9]", "", candidate.name.lower()):
                    vendor = candidate
                    break
        return (vendor.id, vendor.name, vendor.vendor_code) if vendor else None


def _vendor_stock(sender: str, name: str) -> str:
    from openpyxl import Workbook

    from backend.app.integrations.google_sheets.dealer_stock import team_format_table
    from backend.app.integrations.whatsapp.outbound import send_document_safe
    from core.db import get_session

    found = _find_vendor(name or "")
    if found is None:
        return f"'{name}' naam ka vendor nahi mila. Poora naam likhiye."
    vendor_id, vendor_name, code = found
    with get_session() as session:
        headers, table = team_format_table(vendor_id, session)
    if not table:
        return f"{vendor_name} ka abhi koi stock nahi hai."
    book = Workbook()
    sheet = book.active
    sheet.title = (code or "stock")[:31]
    sheet.append(headers)
    for row in table:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    sent = send_document_safe(
        sender,
        buffer.getvalue(),
        f"{code or vendor_name}_stock.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        caption=f"{vendor_name} — {len(table)} part(s)",
    )
    return f"📎 {vendor_name} ka stock bhej diya ({len(table)} part)." if sent else "❌ File bhej nahi paaye, thodi der mein try kijiye."


def _order_status(ref: str) -> str:
    from backend.app.advance_orders import models as m
    from core.db import get_session
    from core.models import Vendor

    digits = re.sub(r"\D", "", ref or "")
    with get_session() as session:
        order = session.execute(select(m.AdvanceOrder).where(m.AdvanceOrder.external_ref == (ref or "").strip())).scalar_one_or_none()
        if order is None and digits:
            order = session.get(m.AdvanceOrder, int(digits))
        if order is None:
            return f"Order '{ref}' nahi mila."
        names = {v.id: v.name for v in session.execute(select(Vendor)).scalars()}
        kind = "DS" if order.kind == m.KIND_DEALER_STOCK else "Advance"
        rows = []
        for line in order.lines:
            who = names.get(line.vendor_id, "") if line.vendor_id else ""
            eta = f", {line.eta_date.strftime('%d %b')}" if line.eta_date else ""
            rows.append(f"• {line.part_number} x{line.qty}: {line.status}{' — ' + who if who else ''}{eta}")
        customer = order.customer_name or order.customer_phone or ""
        return f"{kind} order #{order.id} {('(' + customer + ')') if customer else ''}: {order.status}\n" + "\n".join(rows)


def _confirm_reminder(sender: str) -> str:
    from backend.app.integrations.whatsapp.daily_stock import participation_today
    from core.db import get_session

    with get_session() as session:
        _, pending, _ = participation_today(session)
    if not pending:
        return f"🎉 Sabne aaj stock bhej diya — reminder ki zaroorat nahi."
    return f"{len(pending)} vendors ko stock reminder bhejun? *haan* / *nahi*"


# ------------------------------------------------------------------ entry point
def _pending_action(sender: str) -> str | None:
    for role, text, at in reversed(_recent(sender)):
        if role == "pending":
            if at is not None and now_ist_naive() - at > timedelta(minutes=CONFIRM_MINUTES):
                return None
            return text
        if role == "in":
            continue
        return None
    return None


def handle(sender: str, text: str) -> bool:
    """True when the assistant answered this staff message. Never raises."""
    from backend.app.ai import llm
    from backend.app.integrations.whatsapp.outbound import send_reply_safe

    if not ENABLED or not text or not text.strip():
        return False
    try:
        if _GREETING.match(text):
            _forget(sender)
            reply = "Namaste! " + HELP
            _remember(sender, "out", reply)
            send_reply_safe(sender, reply)
            return True

        pending = _pending_action(sender)
        if pending == "send_reminder" and (_YES.match(text) or _NO.match(text)):
            _remember(sender, "in", text)
            if _YES.match(text):
                from backend.app.integrations.whatsapp.daily_stock import handle_reminder_command

                _remember(sender, "out", "reminder sent")
                handle_reminder_command(sender)  # replies with who was nudged
            else:
                _remember(sender, "out", "reminder cancelled")
                send_reply_safe(sender, "Theek hai, reminder nahi bheja.")
            return True

        if not llm.available():
            return False
        history = "\n".join(f"{'Staff' if role == 'in' else 'Bot'}: {said}" for role, said, _ in _recent(sender) if role in ("in", "out"))
        user = (f"Recent conversation:\n{history}\n\n" if history else "") + f"New message: \"{text[:500]}\""
        data, provider = llm.ask_json(SYSTEM, user, purpose="chat")
        if not data:
            return False
        tool = str(data.get("tool") or "none").strip()
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        logger.info("Staff assistant (%s): %r -> %s %s", provider, text[:80], tool, args)
        if tool == "pending_vendors":
            reply = _pending_vendors()
        elif tool == "received_vendors":
            reply = _received_vendors()
        elif tool == "find_part":
            reply = _find_part(str(args.get("part") or ""))
        elif tool == "vendor_stock":
            reply = _vendor_stock(sender, str(args.get("vendor") or ""))
        elif tool == "order_status":
            reply = _order_status(str(args.get("ref") or ""))
        elif tool == "send_reminder":
            reply = _confirm_reminder(sender)
        elif tool == "help":
            reply = HELP
        else:
            return False  # not ours: the caller sends its usual message
        _remember(sender, "in", text)
        _remember(sender, "out", reply)
        if tool == "send_reminder" and "haan" in reply:
            # Saved LAST, so it is the newest thing in memory when the next
            # message ("haan") arrives.
            _remember(sender, "pending", "send_reminder")
        send_reply_safe(sender, reply)
        return True
    except Exception:  # noqa: BLE001 -- the assistant must never break the bot
        logger.exception("Staff assistant failed on %r from %s.", text[:80], sender)
        return False


def is_staff(sender: str) -> bool:
    try:
        from backend.app.integrations.whatsapp.recipients import internal_file_recipients
        from backend.app.integrations.whatsapp.registry import normalize_number

        return normalize_number(sender) in set(internal_file_recipients())
    except Exception:  # noqa: BLE001
        return False
