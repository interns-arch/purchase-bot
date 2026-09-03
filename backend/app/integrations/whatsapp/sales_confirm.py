"""Turning a sales team stock check into a confirmed order.

THE FLOW (Founder, 3 Sep 2026)
-----------------------------
    Amit:  41341M68P00 x 4, 75700TF0901 x 2
    Bot:   📦 Stock check [A3]
           ✅ 41341M68P00 — AVAILABLE, 6 pcs
           ✅ 75700TF0901 — AVAILABLE, 9 pcs
           Reply "confirm <customer name>" to place this as an order.

    Amit:  confirm Karol Bagh
    Bot:   ✅ Order confirmed for Karol Bagh — 2 part(s).
           The purchase team has the buying list.

The allocation workbook goes to the PURCHASE team only. The sales member is
told the order is placed and nothing else -- no vendor, no price. That split
is the whole point: sales moves fast, purchasing decides where to buy.

WHY EXPLICIT CONFIRM, NOT A TIMED AUTO-ASK
------------------------------------------
A stock check is a QUESTION. Creating an order for every question would
reserve stock nobody asked for, and with ten people asking all day a
periodic "shall I confirm?" becomes noise. So nothing happens until the
member says so. An optional single nudge is available
(`WHATSAPP_SALES_NUDGE_MINUTES`, default off) for members who forget.

WHY THE STOCK IS RE-CHECKED AT CONFIRM TIME
-------------------------------------------
Between the question and the yes, another order may have taken the same
stock. The quoted quantities are a snapshot for DISPLAY only. On confirm the
stock is looked up again and the member is told plainly if it moved --
otherwise the bot would promise something already sold, which is exactly the
failure this whole system exists to prevent.

A quote older than `WHATSAPP_SALES_QUOTE_TTL_MINUTES` (default 60) can no
longer be confirmed: an hour-old answer is not a safe basis for an order.
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from backend.app.integrations.whatsapp import sales_query
from backend.app.integrations.whatsapp.models import SalesQuote
from core.logging_setup import get_logger
from core.time_utils import utcnow_naive

logger = get_logger(__name__)

CONFIRM_WORDS = {"confirm", "confirmed", "yes", "ok", "okay", "haan", "ha", "y", "done", "place"}
CANCEL_WORDS = {"cancel", "no", "nahi", "reject", "drop"}

_CONFIRM_PREFIX = re.compile(
    r"^\s*(confirm(?:ed)?|yes|ok(?:ay)?|haan|ha|done|place)\b[\s,:-]*", re.IGNORECASE
)


@dataclass
class ConfirmRequest:
    """A parsed 'confirm ...' message."""

    is_confirm: bool
    customer_name: str | None = None
    reference: str | None = None


def _settings():
    from backend.app.integrations.whatsapp.config import whatsapp_settings

    return whatsapp_settings


def parse_confirm(text: str | None) -> ConfirmRequest:
    """Read 'confirm', 'confirm Karol Bagh', 'yes A3 Karol Bagh', 'ok'.

    Anything that is not a confirmation word returns `is_confirm=False`, so a
    plain part number is still a fresh stock check."""
    raw = (text or "").strip()
    if not raw:
        return ConfirmRequest(False)

    first = re.split(r"[\s,:-]+", raw.lower())[0]
    if first not in CONFIRM_WORDS:
        return ConfirmRequest(False)

    remainder = _CONFIRM_PREFIX.sub("", raw).strip()
    reference = None
    # An explicit quote reference ("A3") may lead the remainder.
    match = re.match(r"^#?([A-Za-z]\d{1,3})\b[\s,:-]*", remainder)
    if match:
        reference = match.group(1).upper()
        remainder = remainder[match.end():].strip()
    return ConfirmRequest(True, customer_name=remainder or None, reference=reference)


def is_cancel(text: str | None) -> bool:
    first = re.split(r"[\s,:-]+", (text or "").strip().lower())[0] if text else ""
    return first in CANCEL_WORDS


def _new_reference(session) -> str:
    """A short human-quotable id (A3, K7). Unique among quotes still open."""
    live = {
        row.reference
        for row in session.execute(
            select(SalesQuote).where(SalesQuote.status == "PENDING")
        ).scalars()
    }
    for _ in range(200):
        candidate = random.choice(string.ascii_uppercase) + str(random.randint(1, 99))
        if candidate not in live:
            return candidate
    return random.choice(string.ascii_uppercase) + str(random.randint(100, 999))


def record_quote(number: str, member: str | None, answers, session) -> SalesQuote:
    """Store what was quoted so a one-word confirm can rebuild it. Only parts
    that are actually available are worth confirming."""
    # Any earlier open quote from this member is superseded -- a bare
    # "confirm" must never be ambiguous about which check it means.
    for row in session.execute(
        select(SalesQuote).where(
            SalesQuote.whatsapp_number == number, SalesQuote.status == "PENDING"
        )
    ).scalars():
        row.status = "EXPIRED"

    quote = SalesQuote(
        whatsapp_number=number,
        member_name=member,
        reference=_new_reference(session),
        parts=[
            {
                "part_no": a.matched_part_number or a.part_number,
                "quantity": str(a.requested) if a.requested is not None else None,
                "available": str(a.available),
            }
            for a in answers
            if a.is_available
        ],
        status="PENDING",
    )
    session.add(quote)
    session.flush()
    return quote


def find_confirmable(number: str, reference: str | None, session) -> SalesQuote | None:
    """The quote a confirm refers to: the named one, else the most recent
    still open.

    A quote PAST its time-to-live is still returned -- `is_stale()` says so,
    and the caller re-checks the stock and asks again rather than throwing
    the parts away and making the member retype them. Only a quote past the
    hard ceiling (`WHATSAPP_SALES_QUOTE_MAX_AGE_HOURS`) is truly abandoned:
    at some point "send the parts again" really is the right answer."""
    statement = select(SalesQuote).where(
        SalesQuote.whatsapp_number == number, SalesQuote.status == "PENDING"
    )
    if reference:
        statement = statement.where(SalesQuote.reference == reference.upper())
    quote = session.execute(
        statement.order_by(SalesQuote.created_at.desc(), SalesQuote.id.desc())
    ).scalars().first()
    if quote is None:
        return None

    max_hours = _settings().sales_quote_max_age_hours
    if max_hours > 0 and quote.created_at is not None:
        if utcnow_naive() - quote.created_at > timedelta(hours=max_hours):
            quote.status = "EXPIRED"
            return None
    return quote


def is_stale(quote: SalesQuote) -> bool:
    """Whether this quote's numbers are too old to order from directly.

    Stale does NOT mean useless: the parts the member asked about are still
    exactly what they want. It means the QUANTITIES must be looked up again
    and shown before anything is ordered."""
    ttl = _settings().sales_quote_ttl_minutes
    if ttl <= 0 or quote.created_at is None:
        return False
    return utcnow_naive() - quote.created_at > timedelta(minutes=ttl)


def refresh(quote: SalesQuote, answers, session) -> None:
    """Replace a stale quote's quantities with what is available NOW and
    restart its clock, so the member's next "yes" orders against figures
    they have actually been shown."""
    quote.parts = [
        {
            "part_no": a.matched_part_number or a.part_number,
            "quantity": str(a.requested) if a.requested is not None else None,
            "available": str(a.available),
        }
        for a in answers
        if a.is_available
    ]
    quote.created_at = utcnow_naive()
    quote.nudged_at = None
    session.flush()


def quote_as_text(quote: SalesQuote) -> str:
    """The parts on a quote, as an order the text importer can read."""
    lines = []
    for part in quote.parts or []:
        quantity = part.get("quantity")
        lines.append(
            f"{part['part_no']} x {quantity}" if quantity else str(part["part_no"])
        )
    return "\n".join(lines)


def recheck(quote: SalesQuote, session) -> tuple[list, list[str]]:
    """Re-run the stock check for this quote NOW.

    Returns (answers, warnings). A warning is raised for every part whose
    availability dropped since the quote -- the member must hear that before
    the order is placed, not after."""
    answers = sales_query.check_stock(quote_as_text(quote), session)
    quoted = {
        str(part["part_no"]).strip().upper(): Decimal(part["available"])
        for part in (quote.parts or [])
    }
    warnings: list[str] = []
    for answer in answers:
        key = (answer.matched_part_number or answer.part_number).strip().upper()
        was = quoted.get(key)
        if was is None:
            continue
        if answer.available < was:
            if answer.available <= 0:
                warnings.append(f"{answer.part_number} — now SOLD OUT (was {was})")
            else:
                warnings.append(
                    f"{answer.part_number} — now {answer.available} (was {was})"
                )
    return answers, warnings
