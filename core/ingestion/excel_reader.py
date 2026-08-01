"""Excel reading (.xlsx via openpyxl, legacy .xls via xlrd).

Produces the same `ParsedFile` contract as `csv_reader.read_csv_rows` so the
import service can treat CSV and Excel sources uniformly.

Sheet-selection rule: the first sheet with a non-empty header row is used
(this matches the CSV rule that a header-only file is still valid data, not
an error). Every other sheet that has *any* content (header and/or data) is
recorded in `ParsedFile.ignored_sheets` rather than being silently dropped.
Completely blank sheets are skipped without comment.
"""

from __future__ import annotations

from pathlib import Path

from core.ingestion.types import ParsedFile

RawSheet = tuple[str, list[tuple[object, ...]]]


def _is_blank_row(row: tuple[object, ...]) -> bool:
    return all(_clean_cell(value) == "" for value in row)


def _clean_cell(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _trim_trailing_blank_columns(header_row: tuple[object, ...]) -> int:
    """Return the count of header columns up to the last non-empty one."""
    last_non_empty = -1
    for index, value in enumerate(header_row):
        if _clean_cell(value):
            last_non_empty = index
    return last_non_empty + 1


def _select_sheet_and_parse(sheets: list[RawSheet], file_name: str) -> ParsedFile:
    primary_sheet_name: str | None = None
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    ignored_sheets: list[str] = []

    for sheet_name, raw_rows in sheets:
        non_blank_rows = [row for row in raw_rows if not _is_blank_row(row)]

        if not non_blank_rows:
            continue  # completely blank sheet, nothing to report

        if primary_sheet_name is not None:
            ignored_sheets.append(sheet_name)
            continue

        header_row = non_blank_rows[0]
        column_count = _trim_trailing_blank_columns(header_row)
        sheet_headers = [_clean_cell(value) for value in header_row[:column_count]]

        primary_sheet_name = sheet_name
        headers = sheet_headers

        for data_row in non_blank_rows[1:]:
            padded = list(data_row) + [None] * max(0, column_count - len(data_row))
            cleaned_row = {
                sheet_headers[i]: _clean_cell(padded[i]) for i in range(column_count)
            }
            if any(cleaned_row.values()):
                rows.append(cleaned_row)

    if primary_sheet_name is None:
        raise ValueError(f"No header row found in '{file_name}'.")

    return ParsedFile(
        rows=rows,
        headers=headers,
        sheet_name=primary_sheet_name,
        ignored_sheets=ignored_sheets,
    )


def read_xlsx_rows(file_path: Path) -> ParsedFile:
    """Read an Excel .xlsx/.xlsm file."""
    import openpyxl

    workbook = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
    try:
        sheets: list[RawSheet] = [
            (sheet.title, list(sheet.iter_rows(values_only=True)))
            for sheet in workbook.worksheets
        ]
    finally:
        workbook.close()

    return _select_sheet_and_parse(sheets, file_path.name)


def read_xls_rows(file_path: Path) -> ParsedFile:
    """Read a legacy Excel .xls file."""
    import xlrd

    workbook = xlrd.open_workbook(str(file_path))
    sheets: list[RawSheet] = [
        (
            sheet.name,
            [tuple(sheet.row_values(row_index)) for row_index in range(sheet.nrows)],
        )
        for sheet in workbook.sheets()
    ]

    return _select_sheet_and_parse(sheets, file_path.name)


def read_excel_rows(file_path: Path) -> ParsedFile:
    """Dispatch to the correct reader based on file extension."""
    suffix = file_path.suffix.lower()

    if suffix in (".xlsx", ".xlsm"):
        return read_xlsx_rows(file_path)

    if suffix == ".xls":
        return read_xls_rows(file_path)

    raise ValueError(f"Unsupported Excel extension '{suffix}' for '{file_path.name}'.")
