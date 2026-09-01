"""A Customer Order typed as a WhatsApp TEXT message, not sent as a file.

The Founder's ask (1 Sep 2026): after texting `customer`, sending the part
numbers as an ordinary message must do exactly what sending an Excel does --
import the order, allocate it, and reply with the vendor-wise workbook. One
part number or twenty, it makes no difference.

    customer                     <- the existing routing command
    Karol Bagh                   <- optional: the customer name
    41341M68P00 x 4
    75700TF0901 2
    J28283103                    <- no quantity given -> 1

HOW IT WORKS -- AND WHY IT IS SMALL
-----------------------------------
`customer_order_service.run_customer_order_import()` already accepts CSV
(`SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls"}`). So the text is
turned into a two-column CSV and handed to the SAME importer an Excel file
goes through -- no new import path, no change to allocation, and the
vendor-wise workbook (Summary / Shortages / one tab per vendor) comes out
the other end unchanged.

EXCEL IS UNAFFECTED. This is an additional way in, never a replacement: a
text message is only ever read as an order when it actually parses as part
numbers, and files keep their existing route entirely.

WHOSE ORDER IT IS
-----------------
1. A registered CUSTOMER number is the identity -- the registry is
   authoritative and nothing needs to be typed.
2. Otherwise the FIRST LINE may name the customer ("Karol Bagh"), exactly
   like a vendor file's caption names its vendor.
3. Otherwise the order is imported with no customer attached -- already a
   supported state (Gmail orders run that way) and allocation still works.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from core.logging_setup import get_logger

logger = get_logger(__name__)

# The CSV handed to the importer. These exact headers are what
# `core.ingestion.column_detector.find_required_columns` looks for, so the
# synthesized file is read the same way a customer's own sheet is.
CSV_HEADERS = ("Part Number", "Quantity")

# Several parts on one line: "41341M68P00 x 4, 75700TF0901 x 1".
_LINE_SPLIT = re.compile(r"[\n;]+|,(?=\s*[A-Za-z0-9])")

# PART then an optional quantity, separated by x / * / - / : / whitespace.
# The part itself must contain a digit -- that is what stops a customer name
# ("Karol Bagh") or a greeting from being read as a part number.
_PART_QTY = re.compile(
    r"""^\s*
        (?P<part>[A-Za-z0-9][A-Za-z0-9\-_./\\#&*]{2,})   # the part number
        (?:\s*(?:[xX*×:]|-{1,2}|qty|QTY|Qty)?\s*
           (?P<qty>\d+(?:\.\d+)?)\s*(?:nos?|pcs?|pieces?|units?)?
        )?
    \s*$""",
    re.VERBOSE,
)

# Words that are never a part number even though they may carry digits.
_NOT_A_PART = {
    "customer",
    "vendor",
    "invoice",
    "order",
    "qty",
    "quantity",
    "part",
    "parts",
    "urgent",
    "please",
    "thanks",
    "thank",
    "sir",
    "madam",
    "ok",
    "okay",
    "yes",
    "no",
    "good",
    "morning",
    "afternoon",
    "evening",
    "hello",
    "hi",
}


@dataclass
class OrderLine:
    part_number: str
    quantity: Decimal


@dataclass
class ParsedTextOrder:
    """The result of reading a WhatsApp text as an order."""

    lines: list[OrderLine]
    customer_name: str | None = None
    # Lines that looked like they were meant to be parts but could not be
    # read. Reported back to the sender rather than silently dropped -- a
    # missing part is a part nobody buys.
    unreadable: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.unreadable is None:
            self.unreadable = []

    @property
    def is_order(self) -> bool:
        return bool(self.lines)

    @property
    def total_quantity(self) -> Decimal:
        return sum((line.quantity for line in self.lines), Decimal(0))


def _looks_like_part(token: str) -> bool:
    """A part number carries at least one digit and at least three
    characters. `Karol Bagh` and `thanks` never qualify; `J28283103` and
    `41341M68P00` always do."""
    if len(token) < 3:
        return False
    if token.strip().lower() in _NOT_A_PART:
        return False
    return any(character.isdigit() for character in token)


def parse_text_order(text: str | None) -> ParsedTextOrder:
    """Read a WhatsApp text as a list of (part number, quantity).

    Returns a result whose `is_order` is False when the text is not a part
    list at all -- the caller then falls through to the existing text
    handling (routing command, vendor name, instructions), so ordinary
    chatter is never turned into an order."""
    result = ParsedTextOrder(lines=[])
    if not text or not text.strip():
        return result

    raw_lines = [chunk.strip() for chunk in _LINE_SPLIT.split(text) if chunk.strip()]
    if not raw_lines:
        return result

    seen: dict[str, OrderLine] = {}
    for index, chunk in enumerate(raw_lines):
        # The routing command itself may lead the message ("customer\n...").
        if chunk.strip().lower() in {"customer", "order"}:
            continue

        match = _PART_QTY.match(chunk)
        if match and _looks_like_part(match.group("part")):
            part = match.group("part").strip()
            raw_qty = match.group("qty")
            try:
                quantity = Decimal(raw_qty) if raw_qty else Decimal(1)
            except InvalidOperation:
                quantity = Decimal(1)
            if quantity <= 0:
                quantity = Decimal(1)

            # The same part twice in one message is ONE line for the larger
            # quantity, never two rows -- the customer restating a part must
            # not silently double the order.
            existing = seen.get(part.upper())
            if existing is not None:
                existing.quantity = max(existing.quantity, quantity)
            else:
                line = OrderLine(part_number=part, quantity=quantity)
                seen[part.upper()] = line
                result.lines.append(line)
            continue

        # Not a part line. The FIRST such line names the customer (like a
        # vendor file's caption names its vendor); anything later is noise
        # we report rather than drop.
        if index == 0 and result.customer_name is None:
            result.customer_name = chunk
        elif not result.lines:
            # Still before any part line -- could be a multi-word name or a
            # greeting. Keep the first, ignore the rest.
            if result.customer_name is None:
                result.customer_name = chunk
        else:
            result.unreadable.append(chunk)

    # A message that produced no parts is not an order; whatever we guessed
    # as a customer name was just ordinary text.
    if not result.lines:
        result.customer_name = None
        result.unreadable = []
    return result


def build_csv(order: ParsedTextOrder) -> bytes:
    """The two-column CSV handed to `run_customer_order_import`."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADERS)
    for line in order.lines:
        quantity = line.quantity
        # Whole numbers written plainly: 4, not 4.0 -- the importer reads
        # both, but the staged file is kept for audit and gets read by people.
        text = f"{quantity:f}"
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        writer.writerow([line.part_number, text])
    return buffer.getvalue().encode("utf-8")


def summary_reply(order: ParsedTextOrder) -> str:
    """What the sender is told the moment their text is accepted, so a
    misread part number is visible BEFORE the allocation runs."""
    tidy = []
    for line in order.lines:
        quantity = f"{line.quantity:f}"
        if "." in quantity:
            quantity = quantity.rstrip("0").rstrip(".")
        tidy.append(f"{line.part_number} x {quantity}")

    header = f"Got {len(order.lines)} part(s)"
    if order.customer_name:
        header += f" for {order.customer_name}"
    lines = [header + " — checking stock and allocating:", ""]
    lines.extend(tidy[:20])
    if len(tidy) > 20:
        lines.append(f"... and {len(tidy) - 20} more")
    if order.unreadable:
        lines.append("")
        lines.append("⚠️ Could not read (not included):")
        lines.extend(f"• {entry}" for entry in order.unreadable[:5])
    return "\n".join(lines)
