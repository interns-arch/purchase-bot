"""Stock lists sent as a Word document (.docx) or a plain text file (.txt).

Both end up as the same rows an Excel file gives, so every row meets the same
checks (money never becomes quantity, nothing guessed).

.docx  The first table that has a part column and a stock column. A .docx is
       a zip of XML, read here with the standard library -- no extra package
       on the server.
.txt   Tried first as a delimited table (tab, comma, semicolon, pipe -- the
       same sniffing a CSV gets). If no header row is found, read as a typed
       list, one part per line ("16510M68K10 5"), exactly like a WhatsApp
       message -- a part with no quantity is rejected, never imported as 1.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree

from core.ingestion.column_detector import (
    INVENTORY_PART_NUMBER_HEADERS,
    INVENTORY_QUANTITY_HEADERS,
    detect_header_row,
)
from core.ingestion.types import ParsedFile

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_tables(file_path: Path) -> list[list[list[str]]]:
    try:
        with zipfile.ZipFile(file_path) as archive:
            xml = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError(f"'{file_path.name}' is not a readable Word (.docx) file.") from exc
    root = ElementTree.fromstring(xml)
    tables = []
    for table in root.iter(f"{_W}tbl"):
        grid = []
        for row in table.iter(f"{_W}tr"):
            cells = []
            for cell in row.iter(f"{_W}tc"):
                text = "".join(node.text or "" for node in cell.iter(f"{_W}t"))
                cells.append(" ".join(text.split()))
            if any(cells):
                grid.append(cells)
        if grid:
            tables.append(grid)
    return tables


def _rows_from_grid(grid: list[list[str]], name: str) -> ParsedFile | None:
    index = detect_header_row(grid, INVENTORY_PART_NUMBER_HEADERS, quantity_headers=INVENTORY_QUANTITY_HEADERS)
    if index is None:
        return None
    header_row = grid[index]
    width = max((i + 1 for i, v in enumerate(header_row) if str(v).strip()), default=0)
    headers = [str(header_row[i]).strip() for i in range(width)]
    key = [h.lower() for h in headers]
    rows = []
    for raw in grid[index + 1 :]:
        cells = (list(raw) + [""] * width)[:width]
        if not any(cells) or [c.lower() for c in cells] == key:
            continue
        rows.append(dict(zip(headers, cells)))
    return ParsedFile(rows=rows, headers=headers, sheet_name=name)


def docx_grid(file_path: Path) -> list[list[str]]:
    """Every table row in the document, for the header-anywhere path."""
    return [row for table in _docx_tables(file_path) for row in table]


def read_docx_rows(file_path: Path) -> ParsedFile:
    for number, table in enumerate(_docx_tables(file_path), start=1):
        parsed = _rows_from_grid(table, f"table {number}")
        if parsed is not None:
            return parsed
    raise ValueError(
        f"No stock table found in '{file_path.name}'. Expected a table with a "
        "part number column and a stock / quantity column."
    )


def _typed_rows(text: str) -> ParsedFile | None:
    from backend.app.integrations.whatsapp.vendor_text_stock import FRESH_HEADERS, parse_stock_text
    from core.ingestion.column_detector import decimal_to_string

    parsed = parse_stock_text(text)
    if not parsed.is_stock_list:
        return None
    part, desc, qty = FRESH_HEADERS
    rows = [{part: l.part_number, desc: l.description, qty: decimal_to_string(l.quantity)} for l in parsed.lines]
    # Lines that named a part but gave no quantity are kept as rows with a
    # blank quantity, so the importer REJECTS them visibly in Import History
    # instead of them vanishing.
    for line in parsed.unreadable:
        tokens = line.split()
        rows.append({part: tokens[0] if tokens else line, desc: " ".join(tokens[1:]), qty: ""})
    return ParsedFile(rows=rows, headers=list(FRESH_HEADERS), sheet_name="typed list")


def txt_grid(file_path: Path) -> list[list[str]]:
    from core.ingestion.csv_reader import read_csv_grid

    return read_csv_grid(file_path)


def read_txt_rows(file_path: Path) -> ParsedFile:
    from core.ingestion.csv_reader import detect_encoding

    try:
        parsed = _rows_from_grid(txt_grid(file_path), "text table")
    except Exception:  # noqa: BLE001 -- a non-delimited file is tried as a typed list
        parsed = None
    if parsed is not None and parsed.rows:
        return parsed
    text = file_path.read_text(encoding=detect_encoding(file_path), errors="replace")
    typed = _typed_rows(text)
    if typed is not None:
        return typed
    raise ValueError(
        f"No stock found in '{file_path.name}'. Send a table with Part Number and "
        "Stock columns, or one part per line: part number and quantity."
    )
