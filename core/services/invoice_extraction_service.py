"""Vendor Invoice PDF text/table extraction (Vendor Invoice Verification,
step 1). Uses `pdfplumber` for text-based PDFs only -- no OCR in this
phase. A PDF with no extractable text/line items raises
`InvoiceParsingError`, which the caller
(`vendor_invoice_verification_service`) turns into a NEEDS_REVIEW result
rather than a crash, so the original PDF is kept for manual verification
and other invoices in the same batch still get processed.

Kept as its own module, deliberately separate from
`vendor_invoice_verification_service.py`'s business logic (vendor/part
resolution, discrepancy classification against `VendorSelection`), so an
OCR fallback for scanned invoices can be added here later without touching
anything downstream.

Pure business logic -- no FastAPI/print()/input() here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pdfplumber

from core.ingestion.column_detector import (
    PART_NUMBER_HEADERS,
    QUANTITY_HEADERS,
    is_parseable_quantity,
    normalise_header,
    parse_quantity,
)

# A vendor invoice's line items use the same "quantity" vocabulary as an
# inventory/order file, plus a couple of invoice-specific synonyms.
_INVOICE_QUANTITY_HEADERS = QUANTITY_HEADERS | {
    "invoicedqty",
    "invoicedquantity",
    "billedqty",
    "billedquantity",
    "shippedqty",
    "shippedquantity",
}

_VENDOR_LABEL_PATTERN = re.compile(r"(?im)^\s*(?:vendor|supplier|sold\s*by|from)\s*[:\-]\s*(.+)$")

# Fallback for invoices with no detectable table structure: a line that
# starts with a part-number-like token and ends with a numeric quantity.
_TEXT_LINE_PATTERN = re.compile(
    r"^\s*(?P<part>[A-Za-z0-9][A-Za-z0-9\-/.]{2,})\s+.*?(?P<qty>\d+(?:\.\d+)?)\s*$"
)

# Header/metadata lines ("Vendor: Acme", "Invoice Number: INV-0001") would
# otherwise match _TEXT_LINE_PATTERN too (a word followed by a number) and
# get misread as a line item -- these never contain a colon in a real line
# item, and their leading word is one of a small, predictable set of labels.
_LABEL_WORDS = {
    "vendor", "supplier", "invoice", "date", "number", "no", "po",
    "total", "subtotal", "tax", "gst", "amount", "page", "from", "to", "sold",
}


class InvoiceParsingError(Exception):
    """Raised when a PDF has no extractable text/line items at all -- the
    caller records the invoice as NEEDS_REVIEW instead of crashing."""


@dataclass
class ExtractedInvoiceLine:
    part_number: str
    quantity: Decimal
    raw_text: str


@dataclass
class ExtractedInvoice:
    vendor_name: str | None
    lines: list[ExtractedInvoiceLine] = field(default_factory=list)


def _guess_vendor_name(full_text: str) -> str | None:
    match = _VENDOR_LABEL_PATTERN.search(full_text)
    if match:
        candidate = match.group(1).strip()
        if candidate:
            return candidate
    return None


def _extract_from_tables(pdf) -> list[ExtractedInvoiceLine]:
    lines: list[ExtractedInvoiceLine] = []
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            if not table or len(table) < 2:
                continue

            header_row = table[0]
            normalised_headers = [normalise_header(cell) for cell in header_row]
            part_col = next(
                (i for i, h in enumerate(normalised_headers) if h in PART_NUMBER_HEADERS), None
            )
            qty_col = next(
                (i for i, h in enumerate(normalised_headers) if h in _INVOICE_QUANTITY_HEADERS),
                None,
            )
            if part_col is None or qty_col is None:
                continue

            for row in table[1:]:
                if part_col >= len(row) or qty_col >= len(row):
                    continue
                raw_part = (row[part_col] or "").strip()
                raw_qty = (row[qty_col] or "").strip()
                if not raw_part or not is_parseable_quantity(raw_qty):
                    continue
                quantity = parse_quantity(raw_qty)
                if quantity <= 0:
                    continue
                lines.append(
                    ExtractedInvoiceLine(
                        part_number=raw_part,
                        quantity=quantity,
                        raw_text=" | ".join(str(cell) for cell in row if cell),
                    )
                )
    return lines


def _extract_from_text_lines(full_text: str) -> list[ExtractedInvoiceLine]:
    """Best-effort only -- lines that don't match the expected shape are
    simply skipped, not reported as errors."""
    lines: list[ExtractedInvoiceLine] = []
    for raw_line in full_text.splitlines():
        if ":" in raw_line:
            continue

        match = _TEXT_LINE_PATTERN.match(raw_line)
        if not match:
            continue

        part_number = match.group("part")
        if part_number.lower() in _LABEL_WORDS:
            continue

        raw_qty = match.group("qty")
        if not is_parseable_quantity(raw_qty):
            continue
        quantity = parse_quantity(raw_qty)
        if quantity <= 0:
            continue
        lines.append(
            ExtractedInvoiceLine(
                part_number=part_number, quantity=quantity, raw_text=raw_line.strip()
            )
        )
    return lines


def extract_invoice_data(file_path: Path) -> ExtractedInvoice:
    """Raises `InvoiceParsingError` if the PDF has no extractable text at
    all, or no line items could be identified by either the table or
    text-line heuristics."""
    try:
        with pdfplumber.open(file_path) as pdf:
            full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
            if not full_text.strip():
                raise InvoiceParsingError(
                    f"'{file_path.name}' has no extractable text -- likely a scanned/"
                    "image-based PDF (OCR is not supported yet)."
                )

            lines = _extract_from_tables(pdf)
            if not lines:
                lines = _extract_from_text_lines(full_text)
    except InvoiceParsingError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any pdfplumber failure is a parsing failure
        raise InvoiceParsingError(f"Could not read '{file_path.name}' as a PDF: {exc}") from exc

    if not lines:
        raise InvoiceParsingError(
            f"Could not identify any part number / quantity line items in '{file_path.name}'."
        )

    return ExtractedInvoice(vendor_name=_guess_vendor_name(full_text), lines=lines)
