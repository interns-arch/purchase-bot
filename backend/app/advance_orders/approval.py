"""Purchase approval mode (testing, 28 Oct 2026).

For now the bot does only two things: take vendor stock files (unchanged)
and, for every purchase order the sales bot creates, run this approval:

    order created  ->  WhatsApp to Ujjwal sir with the parts
    "APPROVE ADV-..."  ->  sent to Rajkumar and Alam sir, who talk to the
                           vendors themselves
    "REJECT ADV-... reason"  ->  order closed, nobody else is told

No vendor is asked, reminded or told "order pakka" while this is on: the
order never enters the vendor loop (status AWAITING_APPROVAL, never ASKING)
and `service.tick` does nothing.

    PURCHASE_APPROVAL_MODE=true
    PURCHASE_APPROVER_NUMBERS=Ujjwal:918178412512
    PURCHASE_FORWARD_NUMBERS=Rajkumar:919217396458,Alam:919217030408

Settings are read on every call, so a restart is all it takes to switch.
Off -> nothing here runs and orders go to the vendors as before.
"""

from __future__ import annotations

import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as m
from backend.app.integrations.whatsapp.outbound import send_reply_safe
from backend.app.integrations.whatsapp.registry import normalize_number
from backend.app.notifications import broker
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

AWAITING_APPROVAL = "awaiting_approval"
APPROVED = "approved"
REJECTED = "rejected"

_DEFAULT_APPROVERS = "Ujjwal:918178412512"
_DEFAULT_FORWARD = "Rajkumar:919217396458,Alam:919217030408"
# Honorific per first name; anyone else is just named.
_SIR = {"ujjwal", "alam"}

_REPLY = re.compile(
    r"^\s*(?P<verb>approve|approved|reject|rejected)\b[\s:\-,.]*(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)


def enabled() -> bool:
    return (os.environ.get("PURCHASE_APPROVAL_MODE") or "").strip().lower() == "true"


def _people(name: str, default: str) -> dict[str, str]:
    """{number: name} from a "Name:number,Name:number" list."""
    raw = os.environ.get(name)
    out: dict[str, str] = {}
    for item in (default if raw is None else raw).split(","):
        label, _, number = item.strip().rpartition(":")
        number = normalize_number(number)
        if number:
            out[number] = label.strip()
    return out


def approvers() -> dict[str, str]:
    return _people("PURCHASE_APPROVER_NUMBERS", _DEFAULT_APPROVERS)


def forward_to() -> dict[str, str]:
    return _people("PURCHASE_FORWARD_NUMBERS", _DEFAULT_FORWARD)


def _title(name: str) -> str:
    if not name:
        return ""
    return f"{name} sir" if name.split()[0].lower() in _SIR else name


def code(order: m.AdvanceOrder) -> str:
    return order.external_ref or f"PO{order.id}"


def _key(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def order_text(order: m.AdvanceOrder) -> str:
    rows = []
    for i, line in enumerate(order.lines, start=1):
        name = f" — {line.part_name}" if line.part_name else ""
        brand = f" ({line.brand})" if line.brand else ""
        rows.append(f"{i}. {line.part_number}{name}{brand} x{line.qty}")
    head = [f"Purchase order {code(order)}"]
    if order.customer_name or order.customer_phone:
        head.append(f"Customer: {' '.join(x for x in (order.customer_name, order.customer_phone) if x)}")
    if order.requested_by:
        head.append(f"Requested by: {order.requested_by}")
    if order.needed_by:
        head.append(f"Needed by: {order.needed_by:%d %b %Y}")
    return "\n".join(head) + f"\n\nParts ({len(rows)}):\n" + "\n".join(rows)


def _names(names: list[str]) -> str:
    return f"{', '.join(names[:-1])} and {names[-1]}" if len(names) > 1 else "".join(names)


def _send(people: dict[str, str], text: str) -> list[str]:
    sent = []
    for number, name in people.items():
        greeting = f"Namaste {_title(name)},\n" if name else ""
        if send_reply_safe(number, greeting + text):
            sent.append(_title(name) or number)
        else:
            logger.error("Purchase approval: could not reach %s (%s).", name or "-", number)
    return sent


def _publish(level: str, title: str, message: str) -> None:
    # Website toast; only failures are also mirrored to the admins' WhatsApp.
    try:
        broker.publish(level, title, message, mirror=level == "error")
    except Exception:  # noqa: BLE001 -- the website toast is best-effort
        logger.exception("Could not publish '%s'.", title)


# ------------------------------------------------------------------ ask
def hold_for_approval(order: m.AdvanceOrder, session: Session) -> None:
    """Called by `service.create_order` instead of asking the vendors."""
    order.status = AWAITING_APPROVAL
    session.flush()
    ref = code(order)
    text = (
        f"🛒 {order_text(order)}\n\n"
        f"Reply *APPROVE {ref}* to send it to {_names([_title(n) for n in forward_to().values()])}\n"
        f"or *REJECT {ref} <reason>*."
    )
    sent = _send(approvers(), text)
    logger.info("Purchase order %s waiting for approval; asked %s.", ref, ", ".join(sent) or "nobody")
    if not sent:
        _publish("error", "Purchase approval not sent", f"{ref}: the approver could not be reached on WhatsApp.")


# ------------------------------------------------------------------ reply
def _waiting(session: Session) -> list[m.AdvanceOrder]:
    return list(
        session.execute(
            select(m.AdvanceOrder).where(m.AdvanceOrder.status == AWAITING_APPROVAL).order_by(m.AdvanceOrder.id)
        ).scalars()
    )


def _find(word: str, session: Session) -> m.AdvanceOrder | None:
    key = _key(word)
    if not key:
        return None
    for order in session.execute(
        select(m.AdvanceOrder).where(m.AdvanceOrder.status.in_([AWAITING_APPROVAL, APPROVED, REJECTED]))
    ).scalars():
        if key in {_key(code(order)), str(order.id)}:
            return order
    return None


def handle_reply(sender: str, text: str, session: Session) -> bool:
    """An approver's APPROVE / REJECT. Anything else (or anyone else) is not
    claimed, so the normal text handling carries on."""
    if not enabled():
        return False
    number = normalize_number(sender)
    people = approvers()
    if number not in people:
        return False
    match = _REPLY.match(text or "")
    if not match:
        return False
    approve = match["verb"].lower().startswith("approve")
    rest = match["rest"].strip()
    first, _, after = rest.partition(" ")
    order = _find(first, session)
    reason = after.strip() if order else rest
    if order is None:
        waiting = _waiting(session)
        if len(waiting) == 1 and (not first or not approve):
            # "APPROVE" alone, or "REJECT price too high", with one order open.
            order = waiting[0]
        else:
            listed = "\n".join(f"• {code(o)}" for o in waiting) or "none"
            send_reply_safe(
                number,
                f"Which order? Please reply with its number, e.g. "
                f"*{'APPROVE' if approve else 'REJECT'} {code(waiting[0]) if waiting else 'ADV-...'}*.\n"
                f"Waiting for approval:\n{listed}",
            )
            return True

    # First reply wins: lock the row, then look again.
    order = session.execute(
        select(m.AdvanceOrder).where(m.AdvanceOrder.id == order.id).with_for_update()
    ).scalar_one()
    ref = code(order)
    if order.status != AWAITING_APPROVAL:
        send_reply_safe(number, f"{ref} is already {order.status.replace('_', ' ')} — nothing changed.")
        return True

    who = _title(people[number]) or number
    order.confirmed_at = now_ist_naive()
    if approve:
        order.status = APPROVED
        session.flush()
        note = f"✅ {order_text(order)}\n\nApproved by {who}. Please arrange these parts from the vendors."
        sent = _send(forward_to(), note)
        send_reply_safe(
            number,
            f"✅ {ref} approved. Sent to {_names(sent)}." if sent
            else f"✅ {ref} approved, but it could not be sent to the purchase team on WhatsApp.",
        )
        _publish("success" if sent else "error", "Purchase order approved", f"{ref} approved by {who}.")
        logger.info("Purchase order %s approved by %s; sent to %s.", ref, who, ", ".join(sent) or "nobody")
    else:
        order.status = REJECTED
        session.flush()
        send_reply_safe(number, f"❌ {ref} rejected{': ' + reason if reason else ''}. Nobody else was told.")
        _publish("warning", "Purchase order rejected", f"{ref} rejected by {who}{': ' + reason if reason else ''}.")
        logger.info("Purchase order %s rejected by %s: %s", ref, who, reason or "-")
    return True
