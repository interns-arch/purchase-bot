"""Advance-order parts no vendor could answer go to a PERSON (Prateek sir).

When (Founder, 10 Oct 2026):
  - the part's brand is not in the VENDOR BRAND MAPPING, or
  - the brand's vendors did not answer (no reply in the 9 vendor-hours, or
    the message could not reach them).
A vendor who clearly said "no" is still "not available" -- that is an answer.

The escalated lines wait (status `escalated`) while Prateek sir is asked on
WhatsApp. His reply decides them:
  "AO-12 available 3 din"  (or per part, or just "haan 3 din" when only one
                            order is waiting) -> available, the sales bot
                            tells the customer
  "AO-12 not available" / "AO-12 cancel"     -> those parts are cancelled,
                            the sales bot tells the customer
  "AO-12 vendor Sharma Auto 9876543210"     -> that vendor is asked now and
                            saved on the brand's vendor list for next time
No reply within ADVANCE_ORDER_ESCALATION_HOURS vendor-hours -> not found.

The sales bot polls the order, so settling it here is all it takes for the
customer to hear the outcome."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.advance_orders import models as m
from backend.app.advance_orders.config import advance_order_settings as cfg
from core.logging_setup import get_logger
from core.time_utils import now_ist_naive

logger = get_logger(__name__)

_ORDER_REF = re.compile(r"\b(?:AO|ADV|ORDER|#)\s*[-#:]?\s*(\d{1,7})\b", re.IGNORECASE)
# An Indian mobile in Prateek sir's reply = "ask this vendor instead".
_MOBILE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{4}[\s-]?\d{5})(?!\d)")
_NAME_NOISE = re.compile(
    r"\b(vendor|vender|naam|name|contact|mobile|mob|phone|ph|number|num|no|ka|ki|ke|ko|se|hai|h|"
    r"pucho|poocho|puchho|poochho|poochh|lo|ask|try|karo|kar|do|isse|iss|is|this|par|pe|whatsapp|wa)\b\.?",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"\b(cancel+(ed)?|not\s*available|n/?a|nahi+|nahin|nhi|no|unavailable|nai|mat\s*karo|band\s*karo)\b",
    re.IGNORECASE,
)


def enabled() -> bool:
    return bool(cfg.escalation_numbers)


def ref(order: m.AdvanceOrder) -> str:
    return f"AO-{order.id}"


def escalate(order: m.AdvanceOrder, lines: list[m.AdvanceOrderLine], reason: str, session: Session, now: datetime) -> None:
    """Hand these lines to Prateek sir and start his clock."""
    from backend.app.advance_orders import service

    for line in lines:
        line.status = m.LINE_ESCALATED
        line.note = f"asked Prateek sir: {reason}"
    order.deadline_at = service._add_vendor_hours(now, cfg.escalation_hours * 60)
    session.flush()

    who = order.customer_name or order.customer_phone or "customer"
    rows = "\n".join(f"{i}. {line.part_number} ({line.brand}) x{line.qty}" for i, line in enumerate(lines, start=1))
    needed = service._fmt_day(order.needed_by) or "jaldi"
    body = (
        f"🔎 Advance order {ref(order)} ({who}) — {reason}:\n{rows}\n"
        f"Needed by {needed}.\n\n"
        f"Ab kya karna hai? Reply here:\n"
        f"• {ref(order)} vendor Sharma Auto 98XXXXXXXX  (main us vendor se poochh lunga, aur next time ke liye save)\n"
        f"• {ref(order)} cancel  (sales bot customer ko bata dega)\n"
        f"• {ref(order)} available 3 din  (agar aapko pata hai)\n"
        f"No reply in {cfg.escalation_hours} working hours = not found."
    )
    brands = sorted({line.brand for line in lines if line.brand and line.brand != cfg.default_brand})
    parts = "; ".join(f"{line.part_number} x{line.qty}" for line in lines) + f" ({ref(order)})"
    for number in dict.fromkeys(service._normalize(n) for n in cfg.escalation_numbers):
        if not number:
            continue
        # The approved template reaches him even outside WhatsApp's 24-hour
        # window; the full text with the reply options follows.
        if service._vendor_template_ready():
            try:
                service._send_template(
                    number, cfg.vendor_template, cfg.template_language,
                    [", ".join(brands) or "these", parts, needed],
                )
            except Exception:  # noqa: BLE001 -- the text below may still get through
                logger.exception("Advance order %s: escalation template to %s failed.", order.id, number)
        try:
            service._send_text(number, body)
        except Exception:  # noqa: BLE001
            logger.exception("Advance order %s: escalation text to %s failed.", order.id, number)
    logger.info("Advance order %s: %d line(s) escalated to Prateek sir (%s).", order.id, len(lines), reason)


def _open_escalations(session: Session) -> list[m.AdvanceOrder]:
    orders = session.execute(
        select(m.AdvanceOrder).where(m.AdvanceOrder.status == m.ASKING).order_by(m.AdvanceOrder.id.desc())
    ).scalars().all()
    return [o for o in orders if any(line.status == m.LINE_ESCALATED for line in o.lines)]


def handle_reply(sender: str, text: str, session: Session) -> bool:
    """True when this was Prateek sir answering an escalated part."""
    from backend.app.advance_orders import service
    from backend.app.advance_orders.parser import parse_vendor_reply

    number = service._normalize(sender)
    if not number or number not in {service._normalize(n) for n in cfg.escalation_numbers}:
        return False
    waiting = _open_escalations(session)
    if not waiting:
        return False
    body = (text or "").strip()
    match = _ORDER_REF.search(body)
    if match:
        order = next((o for o in waiting if o.id == int(match.group(1))), None)
        if order is None:
            return False  # an order number that is not waiting on him: not ours
    elif len(waiting) == 1:
        order = waiting[0]
    else:
        # Only claim the message if it looks like an answer.
        if not (
            _CANCEL.search(body) or _MOBILE.search(body)
            or re.search(r"\b(haan|ha|yes|available|din|days?|vendor)\b", body, re.I)
        ):
            return False
        service._send_text(
            number,
            "Kaunse order ke liye? Order number ke saath likhiye, jaise:\n"
            + "\n".join(f"• {ref(o)} vendor <naam> <number>   /   {ref(o)} cancel" for o in waiting[:5]),
        )
        return True

    lines = [line for line in order.lines if line.status == m.LINE_ESCALATED]
    before = service._snapshot(order)
    stripped = _ORDER_REF.sub(" ", body)

    # "AO-12 vendor Sharma Auto 9876543210": ask that vendor, and keep him on
    # the brand's list for next time. Checked before cancel -- "contact no"
    # must not read as "no".
    mobile = _MOBILE.search(stripped)
    if mobile:
        digits = re.sub(r"\D", "", mobile.group(1))
        name = _NAME_NOISE.sub(" ", _MOBILE.sub(" ", stripped))
        name = re.sub(r"[^\w&./ -]", " ", name)
        name = re.sub(r"\s{2,}", " ", name).strip(" -.,/")
        vendor = service.assign_vendor(order, lines, name, "91" + digits, session, now_ist_naive())
        service._notify_if_changed(order, before, session)
        brands = ", ".join(sorted({line.brand for line in lines if line.brand != cfg.default_brand})) or "-"
        later = not service._in_vendor_hours(now_ist_naive())
        service._send_text(
            number,
            f"✅ {ref(order)}: {vendor.name} (+91 {digits}) se "
            + ("kal subah vendor hours mein poochhunga" if later else "poochh raha hoon")
            + f" — {', '.join(line.part_number for line in lines)}.\n"
            f"Brand {brands} ki vendor list mein save kar diya, next time inse bhi poochhunga.\n"
            f"{cfg.vendor_wait_minutes // 60} ghante mein reply nahi aaya to aapko phir bataunga.",
        )
        logger.info("Advance order %s: Prateek sir named vendor %s (%s).", order.id, vendor.id, digits)
        return True
    answers = parse_vendor_reply(stripped, [(l.id, l.part_number, l.qty) for l in lines], now_ist_naive().date())
    positive = {lid: a for lid, a in (answers or {}).items() if a.available}
    # "not available" contains "available": a cancel word wins unless the
    # reply also gives a delivery time for some part.
    if _CANCEL.search(stripped) and not any(a.tat_days or a.eta for a in positive.values()):
        positive = {}
        answers = {}

    if positive:
        for line in lines:
            answer = (answers or {}).get(line.id)
            if answer is None:
                continue
            service.set_line_answer(
                order, line.id, answer.available, session,
                available_qty=answer.available_qty, eta=answer.eta, tat_days=answer.tat_days,
            )
            line.note = "available (Prateek sir)" if answer.available else "not available (Prateek sir)"
        outcome = "available"
    elif _CANCEL.search(stripped) or answers:
        for line in lines:
            line.status = m.LINE_UNAVAILABLE
            line.note = "cancelled by Prateek sir"
        outcome = "cancelled"
    else:
        service._send_text(
            number,
            f"{ref(order)}: samajh nahi aaya. Aise likhiye:\n"
            f"• {ref(order)} vendor <naam> <mobile number>\n• {ref(order)} cancel\n"
            f"• {ref(order)} available 3 din",
        )
        return True

    session.flush()
    service._settle(order)
    service._notify_if_changed(order, before, session)
    still = [line for line in order.lines if line.status == m.LINE_ESCALATED]
    done = ", ".join(line.part_number for line in lines if line.status != m.LINE_ESCALATED)
    reply = f"✅ {ref(order)}: {done} — {outcome}. Sales bot customer ko bata dega."
    if still:
        reply += f"\nAbhi baaki: {', '.join(l.part_number for l in still)}"
    service._send_text(number, reply)
    logger.info("Advance order %s: Prateek sir answered (%s).", order.id, outcome)
    return True
