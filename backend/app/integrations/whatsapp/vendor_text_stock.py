"""Vendor stock TYPED as a WhatsApp message instead of sent as a file.

    16510M68K10 5
    2630002752 x 12
    TT-100 oil filter 3 pcs
    1701AAA06701N - 0          <- sold out: stays 0, never becomes 1

Only from a REGISTERED vendor number (the number is the identity, exactly as
for a file). Any other text from that number is still ignored, as before.

WHY THIS IS NOT THE CUSTOMER TYPED-ORDER READER
-----------------------------------------------
`customer_text_order` is right for orders and wrong for stock:
  * no quantity -> 1. For stock that invents a part the vendor never said he
    has. Here a line without a quantity is reported back, never imported.
  * 0 -> 1. For stock, 0 means "sold out" and must stay 0.
  * a line must be exactly PART QTY. Vendors type the name in between
    ("TT-100 oil filter 3"); here the first token is the part, the LAST is the
    quantity, and whatever sits between is the description.

A TYPED LIST UPDATES, IT DOES NOT REPLACE
-----------------------------------------
Every stock import replaces the vendor's whole stock. A vendor with 5,000
parts who types three lines means "these three changed", not "I now have
three parts". So the update is applied INSIDE the vendor's current stock --
his own file layout, with MRP, Rate and every other column kept -- and the
result goes through the same importer an Excel file does: the same checks,
the same Google Sheet, Dealer Portal, top-up and notifications.
`WHATSAPP_VENDOR_TEXT_STOCK_MODE=replace` makes a typed list the whole stock
instead.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from core.ingestion.column_detector import (
    DESCRIPTION_HEADERS,
    decimal_to_string,
    find_inventory_columns,
    find_optional_column,
    is_parseable_stock_quantity,
    normalise_part_number,
    parse_stock_quantity,
)
from core.logging_setup import get_logger

logger = get_logger(__name__)

ENABLED = os.environ.get("WHATSAPP_VENDOR_TEXT_STOCK_ENABLED", "true").strip().lower() == "true"
MODE = os.environ.get("WHATSAPP_VENDOR_TEXT_STOCK_MODE", "merge").strip().lower()

# Headers of the CSV used when a vendor has no stock on file yet (or in
# replace mode). The same three columns the purchase team's own cleaned
# files use, and all three are known to the importer.
FRESH_HEADERS = ("PartNo", "Part Description", "Qty")

# One part per line; ";" also separates. A comma separates two parts only
# when followed by a space, so "1,200" stays one number.
_LINE_SPLIT = re.compile(r"[\n;]+|,(?=\s+\S)")
# "1." / "2)" / "-" / "•" at the start of a line.
_BULLET = re.compile(r"^\s*(?:\d{1,3}[.)]|[-•*>])\s+")
# Separators a vendor puts between the part and the quantity.
_SEPARATORS = {"x", "X", "×", "-", "--", ":", "=", "qty", "Qty", "QTY", "stock", "Stock", "hai"}
_COUNT_WORDS = {"set", "sets", "pcs", "pc", "pieces", "piece", "nos", "no", "nos.", "pair", "pairs", "pkt", "pkts", "box", "boxes", "unit", "units"}

# Words that are never a part number even when the line has digits.
_NOT_A_PART = {
    "stock", "vendor", "invoice", "order", "qty", "quantity", "part", "parts", "total",
    "please", "thanks", "thank", "sir", "madam", "ok", "okay", "yes", "no", "hi", "hello",
    "good", "morning", "today", "aaj", "kal", "date",
}


@dataclass
class StockLine:
    part_number: str
    quantity: Decimal
    description: str = ""


@dataclass
class ParsedStockText:
    lines: list[StockLine] = field(default_factory=list)
    # Lines that look like a part but could not be read -- most often a part
    # with no quantity. Sent back to the vendor, never guessed.
    unreadable: list[str] = field(default_factory=list)

    @property
    def is_stock_list(self) -> bool:
        return bool(self.lines)


def _looks_like_part(token: str) -> bool:
    token = token.strip()
    if len(token) < 3 or token.lower() in _NOT_A_PART:
        return False
    return any(ch.isdigit() for ch in token) and any(ch.isalnum() for ch in token)


def _read_line(chunk: str) -> StockLine | None | str:
    """A StockLine, None for text that is not a part line at all, or the
    chunk itself when it looks like a part line but cannot be read."""
    text = _BULLET.sub("", chunk).strip()
    tokens = text.split()
    if not tokens:
        return None
    part = tokens[0].rstrip(":,-")
    if not _looks_like_part(part):
        return None
    rest = [t for t in tokens[1:]]
    # quantity = the LAST token, or the last two when the final one is a unit
    quantity = None
    if rest and rest[-1].lower().rstrip(".") in _COUNT_WORDS and len(rest) >= 2 and is_parseable_stock_quantity(rest[-2]):
        quantity = parse_stock_quantity(rest[-2] + " " + rest[-1])
        rest = rest[:-2]
    elif rest and is_parseable_stock_quantity(rest[-1]):
        quantity = parse_stock_quantity(rest[-1])
        rest = rest[:-1]
    if quantity is None:
        return chunk.strip()  # a part with no readable quantity
    if quantity < 0:
        return chunk.strip()
    description = " ".join(t for t in rest if t not in _SEPARATORS).strip(" -:=")
    return StockLine(part_number=part, quantity=quantity, description=description)


def parse_stock_text(text: str | None) -> ParsedStockText:
    """Read a WhatsApp text as stock lines. `is_stock_list` is False for
    ordinary chatter, which the caller then ignores exactly as before."""
    result = ParsedStockText()
    if not text or not text.strip():
        return result
    by_key: dict[str, StockLine] = {}
    for chunk in _LINE_SPLIT.split(text):
        if not chunk or not chunk.strip():
            continue
        read = _read_line(chunk)
        if read is None:
            continue  # a heading ("aaj ka stock") or chatter
        if isinstance(read, str):
            result.unreadable.append(read)
            continue
        key = normalise_part_number(read.part_number)
        existing = by_key.get(key)
        if existing is not None:
            # The same part twice is ONE line at the larger figure -- never
            # added together, which would inflate stock.
            existing.quantity = max(existing.quantity, read.quantity)
            existing.description = existing.description or read.description
            continue
        by_key[key] = read
        result.lines.append(read)
    return result


# ------------------------------------------------------------------ applying it
@dataclass
class MergeOutcome:
    csv_bytes: bytes
    updated: list[StockLine]
    added: list[StockLine]
    unchanged_count: int
    mode: str


def _fresh_csv(lines: list[StockLine]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(FRESH_HEADERS)
    for line in lines:
        writer.writerow([line.part_number, line.description, decimal_to_string(line.quantity)])
    return buffer.getvalue().encode("utf-8")


def build_stock_csv(vendor_id: int, parsed: ParsedStockText, session: Session) -> MergeOutcome:
    """The CSV to import: the vendor's current stock with the typed lines
    applied (merge), or just the typed lines (replace / nothing on file)."""
    from core.services.inventory_import_service import get_active_raw_table

    headers, table = ([], []) if MODE == "replace" else get_active_raw_table(vendor_id, session)
    if not headers:
        return MergeOutcome(_fresh_csv(parsed.lines), [], list(parsed.lines), 0, "replace" if MODE == "replace" else "fresh")

    rows = [dict(zip(headers, cells)) for cells in table]
    try:
        part_col, qty_col = find_inventory_columns(headers, "current stock", rows)
    except ValueError:
        # His file on record cannot be re-read (should not happen -- it was
        # imported). Never risk his stock: treat the message as a fresh list
        # only if he has nothing on file, otherwise refuse.
        raise ValueError("The vendor's current stock could not be read back to apply the update.")
    desc_col = find_optional_column(headers, DESCRIPTION_HEADERS)

    wanted = {normalise_part_number(line.part_number): line for line in parsed.lines}
    seen: set[str] = set()
    updated: list[StockLine] = []
    for row in rows:
        key = normalise_part_number(row.get(part_col, ""))
        line = wanted.get(key)
        if line is None:
            continue
        row[qty_col] = decimal_to_string(line.quantity)
        if desc_col and line.description and not str(row.get(desc_col, "")).strip():
            row[desc_col] = line.description
        if key not in seen:
            updated.append(line)
        seen.add(key)

    added: list[StockLine] = []
    for key, line in wanted.items():
        if key in seen:
            continue
        new_row = {header: "" for header in headers}
        new_row[part_col] = line.part_number
        new_row[qty_col] = decimal_to_string(line.quantity)
        if desc_col:
            new_row[desc_col] = line.description
        rows.append(new_row)
        added.append(line)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([row.get(header, "") for header in headers])
    unchanged = len(table) - sum(1 for row in table if normalise_part_number(dict(zip(headers, row)).get(part_col, "")) in seen)
    return MergeOutcome(buffer.getvalue().encode("utf-8"), updated, added, unchanged, "merge")


def reply_text(outcome: MergeOutcome, parsed: ParsedStockText) -> str:
    """What the vendor is told -- every part named, so a mistake is caught."""
    def show(lines: list[StockLine]) -> str:
        shown = ", ".join(f"{l.part_number} {decimal_to_string(l.quantity)}" for l in lines[:15])
        return shown + (f" … +{len(lines) - 15} more" if len(lines) > 15 else "")

    parts = ["✅ Stock updated from your message."]
    if outcome.updated:
        parts.append(f"Changed: {show(outcome.updated)}")
    if outcome.added:
        parts.append(f"Added: {show(outcome.added)}")
    if outcome.mode == "merge":
        parts.append(f"Your other {outcome.unchanged_count} part(s) are unchanged.")
    if parsed.unreadable:
        shown = "; ".join(parsed.unreadable[:5])
        parts.append(f"⚠️ Not read (please send part number and quantity): {shown}")
    return "\n".join(parts)
