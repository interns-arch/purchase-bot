"""Answer the sales team's "is this part in our inventory?" on WhatsApp.

Founder, 3 Sep 2026. A registered sales-team number (`sales_team.py`) sends
one or more part numbers, in whatever shape is natural::

    41341M68P00
    41341M68P00 x 4
    is 17530M62S00 available?
    41341M68P00, 75700TF0901

and gets an immediate answer:

    ✅ 41341M68P00 — AVAILABLE, 6 pcs
    ⚠️ 75700TF0901 — only 2 pcs (you asked for 4)
    ❌ J28283103 — NOT available

WHAT THE SALES TEAM NEVER SEES
------------------------------
The vendor. The reply carries availability and quantity only -- never which
vendor holds the stock, never the price. That is deliberate: the sales team
needs a fast yes/no, and the vendor decision belongs to the purchase team
(the allocation workbook already goes only to internal purchase recipients).

WHERE THE NUMBERS COME FROM
---------------------------
`inventory_import_service.get_master_inventory()` -- the SAME source the
allocation engine reads, so a sales answer can never disagree with what
allocation would do. That means it also honours the Founder's CURRENT-STOCK
rule: a vendor whose file is stale contributes nothing, so the sales team is
never told "available" from a five-day-old sheet.

Quantities are the REMAINING figures (`vendor_stock_service.remaining_quantity`)
-- what is already reserved by other customer orders is subtracted, so two
sales people cannot both be told the same last piece is free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from core.ingestion.column_detector import normalise_part_number
from core.logging_setup import get_logger
from core.services import vendor_stock_service
from core.services.inventory_import_service import get_master_inventory

logger = get_logger(__name__)

# Words a sales person naturally wraps a part number in. Stripped before
# parsing so "is 41341M68P00 available?" reads as a part query.
_NOISE = re.compile(
    r"\b(is|are|do|does|we|you|have|has|any|stock|available|availability|"
    r"in|the|for|please|pls|plz|kindly|check|sir|madam|hai|kya|hain|"
    r"chahiye|batao|dekho|qty|quantity|pcs|pieces|nos)\b[?.!,]*",
    re.IGNORECASE,
)


@dataclass
class StockAnswer:
    part_number: str
    requested: Decimal | None
    available: Decimal
    matched_part_number: str | None = None  # the canonical number we matched

    @property
    def is_available(self) -> bool:
        return self.available > 0

    @property
    def is_short(self) -> bool:
        return (
            self.requested is not None
            and self.available > 0
            and self.available < self.requested
        )


def _clean(text: str | None) -> str:
    """Strip the conversational wrapper, keep the part numbers."""
    return _NOISE.sub(" ", text or "")


# One token: a run of part-number-legal characters. Sales messages are free
# form -- several parts land on ONE line ("41341M68P00 75700TF0901 x 10"), so
# the parts are SCANNED out of the text rather than read line by line the way
# a typed customer order is.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_./\\#&]*")
_QTY_MARKER = {"x", "*", "×", "qty", "-", ":"}


def _is_part_token(token: str) -> bool:
    """A part number: at least 3 characters, containing BOTH a digit and a
    letter. Requiring a letter is what stops a bare quantity ("10") or a
    phone number from being read as a part."""
    if len(token) < 3:
        return False
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return has_digit and has_alpha


def extract_parts(text: str | None) -> list[tuple[str, Decimal | None]]:
    """[(part_number, requested_quantity_or_None)] scanned out of a free-form
    message, in the order written.

    A quantity is taken only when it FOLLOWS a part number, optionally after
    an x / * / qty marker -- so "41341M68P00 x 4" and "41341M68P00 4" both
    give 4, while a bare number with no part before it is ignored."""
    tokens = _TOKEN.findall(_clean(text))
    found: list[tuple[str, Decimal | None]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not _is_part_token(token):
            index += 1
            continue

        quantity: Decimal | None = None
        look = index + 1
        if look < len(tokens) and tokens[look].lower() in _QTY_MARKER:
            look += 1
        if look < len(tokens) and tokens[look].isdigit():
            # Only a plain number -- never another part number.
            quantity = Decimal(tokens[look])
            index = look
        found.append((token, quantity))
        index += 1
    return found


def looks_like_stock_query(text: str | None) -> bool:
    """Whether this message contains at least one part-number-shaped token.
    Ordinary chatter ("good morning sir") is not a query."""
    return bool(extract_parts(text))


def _tidy(value: Decimal) -> str:
    text = f"{value:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def check_stock(text: str | None, session) -> list[StockAnswer]:
    """Look every part number in `text` up against current inventory.

    Reads the same master inventory the allocation engine uses, so the answer
    and the eventual allocation always agree."""
    wanted = extract_parts(text)
    if not wanted:
        return []

    master = get_master_inventory(session)
    # Index by NORMALISED number so a sales person's spacing/dashes don't
    # matter -- "41341-M68P00" finds "41341M68P00".
    by_norm = {
        normalise_part_number(row.canonical_part_number): row for row in master
    }

    answers: list[StockAnswer] = []
    seen: set[str] = set()
    for part_number, requested in wanted:
        norm = normalise_part_number(part_number)
        if norm in seen:  # the same part asked twice in one message
            continue
        seen.add(norm)
        row = by_norm.get(norm)
        if row is None:
            answers.append(
                StockAnswer(
                    part_number=part_number,
                    requested=requested,
                    available=Decimal(0),
                )
            )
            continue

        # REMAINING, not raw: subtract what other orders already reserved, so
        # the same last piece is never promised twice.
        total = Decimal(0)
        for entry in row.vendors:
            remaining = vendor_stock_service.remaining_quantity(
                entry.vendor_id, row.part_id, entry.quantity_available, session
            )
            if remaining > 0:
                total += remaining

        answers.append(
            StockAnswer(
                part_number=part_number,
                requested=requested,
                available=total,
                matched_part_number=row.canonical_part_number,
            )
        )
    return answers


def build_reply(answers: list[StockAnswer], asked_by: str | None = None) -> str:
    """The sales team's reply. Availability and quantity ONLY -- no vendor,
    no price."""
    if not answers:
        return (
            "I could not find a part number in that message. Send the part "
            "number(s), e.g.\n\n41341M68P00\n75700TF0901 x 4"
        )

    lines: list[str] = []
    available = short = missing = 0
    for answer in answers:
        # Echo the canonical number when we matched a differently-written one,
        # so the sales person learns the exact number to quote.
        shown = answer.part_number
        if (
            answer.matched_part_number
            and answer.matched_part_number.strip().upper() != shown.strip().upper()
        ):
            shown = f"{shown} ({answer.matched_part_number})"

        if not answer.is_available:
            missing += 1
            lines.append(f"❌ {shown} — NOT available")
        elif answer.is_short:
            short += 1
            lines.append(
                f"⚠️ {shown} — only {_tidy(answer.available)} pcs "
                f"(you asked for {_tidy(answer.requested)})"
            )
        else:
            available += 1
            suffix = ""
            if answer.requested is not None and answer.requested > 1:
                suffix = f" (you asked for {_tidy(answer.requested)})"
            lines.append(
                f"✅ {shown} — AVAILABLE, {_tidy(answer.available)} pcs{suffix}"
            )

    header = f"📦 Stock check — {len(answers)} part(s)"
    if len(answers) > 1:
        header += f": {available} available, {short} short, {missing} not available"
    return "\n".join([header, ""] + lines)
