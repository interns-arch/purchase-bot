"""CSV reading: encoding/dialect detection and row extraction.

Lifted verbatim (behavior-for-behavior) from the original ``ordermatching.py``
script.
"""

from __future__ import annotations

import csv
from pathlib import Path

from core.ingestion.types import ParsedFile

# Fallback encodings that indicate the file wasn't cleanly decodable in a
# "trusted" encoding and should be flagged, not silently accepted.
_FALLBACK_ENCODING = "latin-1"


def detect_encoding(file_path: Path) -> str:
    """
    Select a practical CSV encoding.
    UTF-8-SIG handles files exported by Excel with a BOM.
    """
    encodings = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

    for encoding in encodings:
        try:
            with file_path.open("r", encoding=encoding, newline="") as file:
                file.read(4096)
            return encoding
        except UnicodeDecodeError:
            continue

    return "latin-1"


def detect_csv_dialect(file_path: Path, encoding: str) -> csv.Dialect:
    """
    Detect comma, semicolon, tab or pipe-separated CSV files.
    """
    with file_path.open("r", encoding=encoding, newline="") as file:
        sample = file.read(8192)

    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def read_csv_rows(file_path: Path) -> ParsedFile:
    """
    Read a CSV file and return its rows, headers, and encoding metadata.
    """
    encoding = detect_encoding(file_path)
    dialect = detect_csv_dialect(file_path, encoding)

    with file_path.open("r", encoding=encoding, newline="") as file:
        reader = csv.DictReader(file, dialect=dialect)

        if not reader.fieldnames:
            raise ValueError(f"No header row found in '{file_path.name}'.")

        headers = [
            str(header).strip() if header is not None else ""
            for header in reader.fieldnames
        ]

        rows: list[dict[str, str]] = []

        for raw_row in reader:
            cleaned_row = {
                str(key).strip(): str(value).strip() if value is not None else ""
                for key, value in raw_row.items()
                if key is not None
            }

            # Ignore completely empty rows.
            if any(value for value in cleaned_row.values()):
                rows.append(cleaned_row)

        return ParsedFile(
            rows=rows,
            headers=headers,
            encoding=encoding,
            used_fallback_encoding=(encoding == _FALLBACK_ENCODING),
        )
