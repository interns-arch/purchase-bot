"""Reading a vendor's reply with AI -- only when the fixed rules could not.

The fixed reader (`parser.parse_vendor_reply`) is instant, free and tested,
so it goes first. When it cannot read a reply -- "bhai 3 piece hai baaki next
week", "MRP pe 18% de dunga, kal tak" -- the AI is asked what the vendor
meant, and its answer is then CHECKED against the vendor's own words:

  * every quantity, number of days, rate, MRP and day-of-month it returns
    must be written in the reply (as digits, or as a word like "teen" /
    "kal" / "hafta"). One number the vendor never wrote and the whole answer
    is thrown away -- the sales bot's rule: a number the source never gave
    is never used;
  * only parts that were actually asked about are accepted;
  * a rate is taken only from a vendor who was asked for one.

Anything thrown away goes to the admins exactly as before AI existed.
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from backend.app.advance_orders.parser import LineAnswer
from core.logging_setup import get_logger

logger = get_logger(__name__)

ENABLED = os.environ.get("AI_REPLY_READER_ENABLED", "true").strip().lower() == "true"

_WORD_NUMBERS = {
    "ek": 1, "one": 1, "do": 2, "two": 2, "teen": 3, "tin": 3, "three": 3, "char": 4, "chaar": 4,
    "four": 4, "paanch": 5, "panch": 5, "five": 5, "chhe": 6, "six": 6, "saat": 7, "seven": 7,
    "aath": 8, "eight": 8, "nau": 9, "nine": 9, "das": 10, "ten": 10,
}

SYSTEM = (
    "You read an auto-parts vendor's WhatsApp reply to a purchase enquiry from Cartrends. "
    "Vendors write Hinglish, short forms and typos. Reply with JSON only:\n"
    '{"unclear": false, "answers": [{"part": "<an asked part number>", "available": true, '
    '"quantity": null, "days": null, "date": null, "rate": null, "mrp": null}]}\n'
    "Rules:\n"
    "- Use ONLY what the vendor wrote. Never guess or calculate a number.\n"
    "- quantity: only when he says he has a specific number (sirf 3, 4 piece, 2 set). Otherwise null.\n"
    "- days: delivery time in days if said (kal=1, parso=2, aaj/ready=0, hafta/week=7, 2 hafte=14). Otherwise null.\n"
    "- date: YYYY-MM-DD only if he names a date (20 sep tak). Otherwise null.\n"
    "- rate: price per piece only if he states one. mrp: only if he says MRP.\n"
    "- If he answers for all parts together (sab mil jayega 5 din), give an answer for every asked part.\n"
    "- Leave out a part he does not mention.\n"
    "- If the message is a question, a greeting, or you cannot tell, set unclear true and answers []."
)


def _numbers_in(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for token in re.findall(r"\d+(?:[.,]\d+)?", text):
        try:
            found.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            pass
    for word in re.findall(r"[a-zA-Z]+", text.lower()):
        if word in _WORD_NUMBERS:
            found.add(Decimal(_WORD_NUMBERS[word]))
    return found


def _allowed_days(text: str) -> set[int]:
    lowered = text.lower()
    days = {int(n) for n in _numbers_in(text) if n == n.to_integral_value()}
    if re.search(r"\b(aaj|today|ready|turant|abhi)\b", lowered):
        days.add(0)
    if re.search(r"\b(kal|tomorrow)\b", lowered):
        days.add(1)
    if re.search(r"\b(parso|parson)\b", lowered):
        days.add(2)
    if re.search(r"\b(hafta|hafte|haftey|week|weeks)\b", lowered):
        days.add(7)
        days.update(7 * d for d in list(days) if 0 < d <= 8)
    return days


def _key(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def read(text: str, lines: list[tuple[int, str, int]], today: date, want_rate: bool) -> dict[int, LineAnswer] | None:
    """{line_id: LineAnswer}, or None when AI is off, unavailable, unsure, or
    returned anything that cannot be traced to the vendor's words."""
    from backend.app.ai import llm

    if not (ENABLED and llm.available()) or not text or not lines:
        return None
    asked = "\n".join(f"{i}) {part} x{qty}" for i, (_lid, part, qty) in enumerate(lines, start=1))
    user = (
        f"Today: {today.isoformat()}\n"
        f"Parts we asked about:\n{asked}\n"
        f"{'We asked him for a rate.' if want_rate else 'We did NOT ask him for a rate.'}\n"
        f'Vendor\'s reply: "{text[:800]}"'
    )
    data, provider = llm.ask_json(SYSTEM, user, purpose="reply")
    if not data or data.get("unclear") or not isinstance(data.get("answers"), list):
        return None

    by_key = {_key(part): (lid, qty) for lid, part, qty in lines}
    numbers = _numbers_in(text)
    days_ok = _allowed_days(text)
    out: dict[int, LineAnswer] = {}

    def traced(value) -> Decimal | None | bool:
        """Decimal when it is in his words, None when absent, False when invented."""
        if value in (None, "", False):
            return None
        try:
            number = Decimal(str(value))
        except InvalidOperation:
            return False
        return number if number in numbers else False

    for answer in data["answers"]:
        if not isinstance(answer, dict):
            return None
        target = by_key.get(_key(answer.get("part")))
        if target is None:
            logger.info("AI reply reader named a part that was not asked (%r) -- not used.", answer.get("part"))
            return None
        line_id, _qty = target
        available = answer.get("available")
        if available is False:
            out[line_id] = LineAnswer(available=False)
            continue
        if available is not True:
            return None

        quantity = traced(answer.get("quantity"))
        rate = traced(answer.get("rate")) if want_rate else None
        mrp = traced(answer.get("mrp"))
        if quantity is False or rate is False or mrp is False:
            logger.info("AI reply reader returned a number the vendor never wrote -- not used (%s).", provider)
            return None

        eta, tat = None, None
        days = answer.get("days")
        if days not in (None, ""):
            try:
                days = int(days)
            except (TypeError, ValueError):
                return None
            if days not in days_ok:
                logger.info("AI reply reader invented a delivery time (%s days) -- not used.", days)
                return None
            tat, eta = days, today + timedelta(days=days)
        when = answer.get("date")
        if when and eta is None:
            try:
                parsed = date.fromisoformat(str(when))
            except ValueError:
                return None
            if Decimal(parsed.day) not in numbers or parsed < today:
                return None
            eta, tat = parsed, (parsed - today).days

        out[line_id] = LineAnswer(
            available=True,
            available_qty=int(quantity) if quantity is not None else None,
            eta=eta,
            tat_days=tat,
            quoted_rate=rate,
            mrp=mrp,
        )
    if out:
        logger.info("AI reply reader (%s) read %d line(s) the fixed rules could not.", provider, len(out))
    return out or None
