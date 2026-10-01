"""Reading a vendor's stock list sent as a PDF, and telling it from a bill.

Vendors export stock from their dealer software as Excel, and sometimes as
PDF. A PDF reaches the SAME importer an Excel file does: its table is pulled
out into rows, the header row is found the same way (metadata rows above it
skipped), and every row then meets the same checks -- a money column can
never become a quantity, a part without a number is rejected, nothing is
guessed.

Text-based PDFs only (what dealer software prints). A scanned page has no
text to read; `PdfHasNoText` says so, and the caller treats it as a photo.

STOCK LIST OR BILL?
-------------------
A vendor sends both as PDF: his stock list, and later his invoice for goods
supplied. `classify_pdf` decides from what the PDF SAYS, never from its name:

  bill   "Tax Invoice", "Invoice No", CGST / SGST / IGST, "Taxable",
         "Grand Total", "Amount in words", "Place of supply", e-way / IRN
  stock  "Closing Stock", "Current Stock", "Stock Statement / Status /
         Report", "Net Stock", "Balance Qty", "On hand"

A plain "Qty" column decides nothing -- invoices have one too. When the
signals disagree or are absent the answer is "unclear", and the caller asks
the vendor rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.ingestion.column_detector import (
    INVENTORY_PART_NUMBER_HEADERS,
    INVENTORY_QUANTITY_HEADERS,
    detect_header_row,
)
from core.ingestion.types import ParsedFile


class PdfHasNoText(ValueError):
    """A scanned or image-only PDF: nothing to read without OCR."""


_INVOICE_MARKERS = (
    r"tax\s*invoice",
    r"invoice\s*(no|number|#|date)",
    r"bill\s*(no|number)\b",
    r"bill\s*of\s*supply",
    r"\bcgst\b",
    r"\bsgst\b",
    r"\bigst\b",
    r"taxable",
    r"grand\s*total",
    r"amount\s*in\s*words",
    r"place\s*of\s*supply",
    r"e-?\s*way\s*bill",
    r"\birn\b",
    r"consignee",
    r"buyer'?s?\s*order",
)
_STOCK_MARKERS = (
    r"closing\s*stock",
    r"current\s*stock",
    r"stock\s*(statement|status|report|list|summary|position|details)",
    r"net\s*stock",
    r"balance\s*(qty|quantity|stock)",
    r"available\s*(stock|qty)",
    r"on\s*hand",
    r"inventory\s*(report|list|statement)",
    r"part\s*search\s*details",  # the Maruti DMS export title
)


@dataclass
class PdfContent:
    text: str
    grid: list[list[str]]

    @property
    def has_text(self) -> bool:
        return bool(self.text.strip())


def _clean(cell) -> str:
    return " ".join(str(cell).split()) if cell is not None else ""


def read_pdf(file_path: Path) -> PdfContent:
    """Every page's text, and every table row in page order. Bordered tables
    are read by their ruling lines; a page whose table has no lines (common
    in plain dealer reports) is read by text alignment instead."""
    import pdfplumber

    text_parts: list[str] = []
    grid: list[list[str]] = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text_parts.append(page.extract_text() or "")
            tables = page.extract_tables() or []
            if not any(len(t) > 1 for t in tables):
                tables = page.extract_tables(
                    {"vertical_strategy": "text", "horizontal_strategy": "text"}
                ) or []
            for table in tables:
                for row in table:
                    cells = [_clean(c) for c in row]
                    if any(cells):
                        grid.append(cells)
    return PdfContent(text="\n".join(text_parts), grid=grid)


def classify_pdf(content: PdfContent) -> tuple[str, str]:
    """("stock" | "invoice" | "unclear" | "scanned", why) -- see module doc."""
    if not content.has_text:
        return "scanned", "the PDF has no text (a scanned page or a photo)"
    text = content.text.lower()
    invoice_hits = [m for m in _INVOICE_MARKERS if re.search(m, text)]
    stock_hits = [m for m in _STOCK_MARKERS if re.search(m, text)]
    has_table = _find_header(content.grid) is not None

    if stock_hits and len(invoice_hits) <= 1 and has_table:
        return "stock", f"stock words ({len(stock_hits)}) and a part/stock table"
    if len(invoice_hits) >= 2 and not stock_hits:
        return "invoice", f"invoice words ({len(invoice_hits)})"
    if not has_table and invoice_hits:
        return "invoice", "no stock table; invoice words present"
    return "unclear", (
        f"{len(stock_hits)} stock word(s), {len(invoice_hits)} invoice word(s), "
        f"{'a' if has_table else 'no'} part/stock table"
    )


def _find_header(grid: list[list[str]]) -> int | None:
    if not grid:
        return None
    return detect_header_row(
        grid, INVENTORY_PART_NUMBER_HEADERS, quantity_headers=INVENTORY_QUANTITY_HEADERS
    )


def pdf_grid(file_path: Path) -> list[list[str]]:
    """The raw table grid, for the importer's header-anywhere path."""
    content = read_pdf(file_path)
    if not content.has_text:
        raise PdfHasNoText(
            f"'{file_path.name}' has no text to read (a scanned page). "
            "Send it as Excel, type the list, or send a clear photo."
        )
    return content.grid


def read_pdf_rows(file_path: Path) -> ParsedFile:
    """The PDF's stock table as the same `ParsedFile` the Excel and CSV
    readers return. The header row is found anywhere in the table (title
    rows above it are skipped); a header repeated at the top of every page
    is dropped rather than read as a part."""
    grid = pdf_grid(file_path)
    header_index = _find_header(grid)
    if header_index is None:
        from core.ingestion.column_inference import detect_header_row_by_data

        header_index = detect_header_row_by_data(grid)
    if header_index is None:
        raise ValueError(
            f"Part-number column not found in '{file_path.name}'. "
            "No stock table was found in the PDF."
        )
    header_row = grid[header_index]
    width = max((i + 1 for i, v in enumerate(header_row) if str(v).strip()), default=0)
    raw_headers = [str(header_row[i]).strip() for i in range(width)]

    # A table WITHOUT ruling lines is cut into columns by text alignment, and
    # a description such as "HEAD LAMP 2" can be cut in two when its last
    # word happens to line up down the page -- leaving a column with no
    # header. Such a column is folded back into the DESCRIPTION column on its
    # left. Only a description: folding into a part-number or quantity column
    # would glue unrelated text onto a part number or a count.
    from core.ingestion.column_detector import DESCRIPTION_HEADERS, normalise_header

    describe = {normalise_header(h) for h in DESCRIPTION_HEADERS} | {"name", "partname", "itemname"}
    target: list[int] = []  # for each source column, the column it lands in
    for index, header in enumerate(raw_headers):
        if not header and target:
            left = target[-1]
            if normalise_header(raw_headers[left]) in describe:
                target.append(left)
                continue
        target.append(index)
    kept = sorted(set(target))
    headers = [raw_headers[i] for i in kept]
    header_key = [h.lower() for h in headers]

    rows: list[dict[str, str]] = []
    for raw in grid[header_index + 1 :]:
        cells = (list(raw) + [""] * width)[:width]
        merged = {i: [] for i in kept}
        for index, cell in enumerate(cells):
            if cell:
                merged[target[index]].append(cell)
        values = [" ".join(merged[i]) for i in kept]
        if not any(values):
            continue
        if [v.lower() for v in values] == header_key:
            continue  # the header repeated on the next page
        rows.append(dict(zip(headers, values)))
    return ParsedFile(rows=rows, headers=headers, sheet_name="pdf")
