"""Token-bounded representation of a document for a provider.

Never send a raw workbook/PDF: a 5 000-row inventory file is >100k tokens and
almost all of it is redundant for the only question we ask ("which column is
the part number, which is the quantity, and where does the table start?").

`compact_grid` renders a bounded sample -- the first N rows (which contain any
metadata block plus the header and enough data rows to disambiguate columns),
with long cells truncated -- plus the true row/column counts so the model knows
it is seeing a sample. `compact_pdf_text` does the equivalent for extracted PDF
text.

Pure string formatting; no network, no SDK, no database.
"""

from __future__ import annotations

MAX_CELL_CHARS = 40


def _cell(value: object) -> str:
    text = "" if value is None else str(value).strip()
    text = text.replace("\n", " ").replace("\t", " ")
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 1] + "…"
    return text


def compact_grid(
    grid: list[list[str]],
    *,
    file_name: str,
    max_rows: int = 40,
    max_columns: int = 25,
) -> str:
    """Render a raw grid (from `read_csv_grid` / `read_excel_grid`) as a
    bounded, tab-separated sample with 0-based row indices."""
    total_rows = len(grid)
    total_columns = max((len(row) for row in grid), default=0)

    lines = [
        f"FILE: {file_name}",
        f"TOTAL_ROWS: {total_rows}  TOTAL_COLUMNS: {total_columns}",
        f"SHOWING: first {min(max_rows, total_rows)} rows, first {min(max_columns, total_columns)} columns",
        "ROWS (index: tab-separated cells):",
    ]
    for index, row in enumerate(grid[:max_rows]):
        cells = [_cell(value) for value in row[:max_columns]]
        while cells and cells[-1] == "":
            cells.pop()
        lines.append(f"{index}: " + "\t".join(cells))

    if total_rows > max_rows:
        lines.append(f"... ({total_rows - max_rows} more rows not shown)")
    return "\n".join(lines)


def compact_pdf_text(
    text: str,
    *,
    file_name: str,
    max_chars: int = 12_000,
) -> str:
    """Bound extracted PDF text (from pdfplumber) before sending it on."""
    cleaned = (text or "").strip()
    truncated = len(cleaned) > max_chars
    if truncated:
        cleaned = cleaned[:max_chars]
    header = f"FILE: {file_name}\nEXTRACTED_TEXT_CHARS: {len(text or '')}"
    if truncated:
        header += f" (truncated to {max_chars})"
    return f"{header}\n---\n{cleaned}"
