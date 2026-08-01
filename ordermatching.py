from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
RAW_FILES_DIR = BASE_DIR / "raw_files"
INPUT_FILE = BASE_DIR / "input.csv"
OUTPUT_FILE = BASE_DIR / "matching_output.csv"

# The script searches for these possible header names.
PART_NUMBER_HEADERS = {
    "partno",
    "partnumber",
    "partnum",
    "partcode",
    "itemcode",
    "itemnumber",
    "sku",
    "productcode",
}

QUANTITY_HEADERS = {
    "quantity",
    "qty",
    "availablequantity",
    "availableqty",
    "stockquantity",
    "stockqty",
    "orderedquantity",
    "orderedqty",
}


# ---------------------------------------------------------------------------
# DATA STRUCTURES
# ---------------------------------------------------------------------------

@dataclass
class SourceRow:
    file_name: str
    vendor_name: str
    row_number: int
    part_number: str
    available_quantity: Decimal
    original_data: dict[str, str]


# ---------------------------------------------------------------------------
# HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def normalise_header(value: str | None) -> str:
    """
    Convert headers such as 'Part No.', 'PART_NO' and 'part no'
    into the comparable value 'partno'.
    """
    if value is None:
        return ""

    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def normalise_part_number(value: Any) -> str:
    """
    Normalise a part number for matching.

    Matching is:
    - case-insensitive
    - unaffected by leading/trailing spaces
    - unaffected by spaces, hyphens, underscores and dots

    Examples:
        ABC-123  -> ABC123
        abc 123  -> ABC123
    """
    if value is None:
        return ""

    cleaned = str(value).strip().upper()
    return re.sub(r"[\s\-_.]+", "", cleaned)


def parse_quantity(value: Any) -> Decimal:
    """
    Convert a quantity value into Decimal.

    Supports values such as:
        10
        10.5
        1,000
        " 25 "
    """
    if value is None:
        return Decimal("0")

    text = str(value).strip()

    if not text:
        return Decimal("0")

    # Remove comma-based thousand separators.
    text = text.replace(",", "")

    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def decimal_to_string(value: Decimal) -> str:
    """
    Format Decimal without unnecessary trailing zeroes.
    """
    if value == value.to_integral_value():
        return str(int(value))

    return format(value.normalize(), "f")


def vendor_name_from_file(file_path: Path) -> str:
    """
    Derive a vendor's display name from its file name.

    Mirrors the convention used by inventory_import.py: each raw file
    represents one vendor, identified by the file's stem.
    """
    return file_path.stem.strip()


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


def find_required_columns(
    headers: list[str],
    file_name: str,
) -> tuple[str, str]:
    """
    Find the part-number and quantity columns from a CSV header.
    """
    header_lookup = {
        normalise_header(header): header
        for header in headers
        if header is not None
    }

    part_number_column = next(
        (
            original_header
            for normalised, original_header in header_lookup.items()
            if normalised in PART_NUMBER_HEADERS
        ),
        None,
    )

    quantity_column = next(
        (
            original_header
            for normalised, original_header in header_lookup.items()
            if normalised in QUANTITY_HEADERS
        ),
        None,
    )

    if not part_number_column:
        raise ValueError(
            f"Part-number column not found in '{file_name}'. "
            f"Headers found: {headers}"
        )

    if not quantity_column:
        raise ValueError(
            f"Quantity column not found in '{file_name}'. "
            f"Headers found: {headers}"
        )

    return part_number_column, quantity_column


def read_csv_rows(file_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """
    Read a CSV file and return its rows and headers.
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
                str(key).strip(): str(value).strip()
                if value is not None
                else ""
                for key, value in raw_row.items()
                if key is not None
            }

            # Ignore completely empty rows.
            if any(value for value in cleaned_row.values()):
                rows.append(cleaned_row)

        return rows, headers


# ---------------------------------------------------------------------------
# RAW FILE INDEXING
# ---------------------------------------------------------------------------

@dataclass
class ScanResult:
    part_index: dict[str, list[SourceRow]]
    source_headers: list[str]
    skipped_files: list[str]
    processed_file_count: int
    vendor_names: set[str]


def scan_raw_files() -> ScanResult:
    """
    Scan every CSV in raw_files and build an in-memory index:

        normalised part number -> matching source rows

    The second returned value contains every distinct source CSV header so
    that source information can be added to the output.
    """
    if not RAW_FILES_DIR.exists():
        raise FileNotFoundError(
            f"Raw-files directory does not exist: {RAW_FILES_DIR}"
        )

    csv_files = sorted(
        file_path
        for file_path in RAW_FILES_DIR.glob("*.csv")
        if file_path.is_file()
    )

    if not csv_files:
        raise FileNotFoundError(
            f"No CSV files found in: {RAW_FILES_DIR}"
        )

    part_index: dict[str, list[SourceRow]] = defaultdict(list)
    all_source_headers: list[str] = []
    skipped_files: list[str] = []
    vendor_names: set[str] = set()
    processed_file_count = 0

    print(f"Scanning {len(csv_files)} CSV file(s) from raw_files...")

    for file_path in csv_files:
        try:
            rows, headers = read_csv_rows(file_path)

            part_column, quantity_column = find_required_columns(
                headers,
                file_path.name,
            )

            for header in headers:
                if header not in all_source_headers:
                    all_source_headers.append(header)

            vendor_name = vendor_name_from_file(file_path)
            indexed_count = 0

            for row_number, row in enumerate(rows, start=2):
                original_part_number = row.get(part_column, "").strip()
                part_key = normalise_part_number(original_part_number)

                if not part_key:
                    continue

                available_quantity = parse_quantity(
                    row.get(quantity_column, "")
                )

                source_row = SourceRow(
                    file_name=file_path.name,
                    vendor_name=vendor_name,
                    row_number=row_number,
                    part_number=original_part_number,
                    available_quantity=available_quantity,
                    original_data=row,
                )

                part_index[part_key].append(source_row)
                indexed_count += 1

            vendor_names.add(vendor_name)
            processed_file_count += 1

            print(
                f"  Loaded: {file_path.name} "
                f"(vendor='{vendor_name}', "
                f"{indexed_count} usable row(s), "
                f"part column='{part_column}', "
                f"quantity column='{quantity_column}')"
            )

        except Exception as error:
            message = f"{file_path.name}: {error}"
            skipped_files.append(message)
            print(f"  Skipped: {message}")

    return ScanResult(
        part_index=dict(part_index),
        source_headers=all_source_headers,
        skipped_files=skipped_files,
        processed_file_count=processed_file_count,
        vendor_names=vendor_names,
    )


# ---------------------------------------------------------------------------
# INPUT PROCESSING AND MATCHING
# ---------------------------------------------------------------------------

def load_input_file() -> tuple[list[dict[str, str]], str, str]:
    """
    Read input.csv and identify its part-number and quantity columns.
    """
    if not INPUT_FILE.exists():
        raise FileNotFoundError(
            f"Input file does not exist: {INPUT_FILE}"
        )

    rows, headers = read_csv_rows(INPUT_FILE)

    part_column, quantity_column = find_required_columns(
        headers,
        INPUT_FILE.name,
    )

    return rows, part_column, quantity_column


@dataclass
class PartSummary:
    part_number: str
    category: str  # "MATCHED", "INSUFFICIENT", "NOT_FOUND" or "INVALID"


def match_orders(
    input_rows: list[dict[str, str]],
    input_part_column: str,
    input_quantity_column: str,
    part_index: dict[str, list[SourceRow]],
    source_headers: list[str],
) -> tuple[list[dict[str, str]], list[PartSummary]]:
    """
    Match every input row against every vendor row that carries the part.

    Reporting rule (one output row per matching vendor, not just the best
    one):
    1. Part number must match.
    2. Every vendor that stocks the part is reported, sorted with the
       best-stocked vendor first.
    3. CAN_FULFILL           - vendor's available quantity >= requested
       quantity.
    4. INSUFFICIENT_QUANTITY - vendor has the part but not enough of it.
    5. PART_NOT_FOUND        - no vendor carries the part at all.
    """
    output_rows: list[dict[str, str]] = []
    part_summaries: list[PartSummary] = []

    def blank_source_columns() -> dict[str, str]:
        return {f"source_{header}": "" for header in source_headers}

    def no_vendor_row(
        asked_part_number: str,
        asked_quantity: Decimal,
        status: str,
    ) -> dict[str, str]:
        row = {
            "customer_part_number": asked_part_number,
            "customer_quantity": decimal_to_string(asked_quantity),
            "vendor_name": "-",
            "vendor_file": "-",
            "vendor_available_quantity": "-",
            "can_fulfill": "-",
            "status": status,
            "source_row_number": "",
        }
        row.update(blank_source_columns())
        return row

    for input_row_number, input_row in enumerate(input_rows, start=2):
        asked_part_number = input_row.get(input_part_column, "").strip()
        asked_quantity = parse_quantity(
            input_row.get(input_quantity_column, "")
        )

        if not asked_part_number:
            output_rows.append(
                no_vendor_row(
                    asked_part_number,
                    asked_quantity,
                    "INVALID_INPUT_PART_NUMBER",
                )
            )
            part_summaries.append(PartSummary(asked_part_number, "INVALID"))
            print(
                f"Input row {input_row_number}: "
                f"(blank part number) | INVALID_INPUT_PART_NUMBER"
            )
            continue

        if asked_quantity <= 0:
            output_rows.append(
                no_vendor_row(
                    asked_part_number,
                    asked_quantity,
                    "INVALID_INPUT_QUANTITY",
                )
            )
            part_summaries.append(PartSummary(asked_part_number, "INVALID"))
            print(
                f"Input row {input_row_number}: "
                f"{asked_part_number} | INVALID_INPUT_QUANTITY"
            )
            continue

        part_key = normalise_part_number(asked_part_number)
        candidates = part_index.get(part_key, [])

        if not candidates:
            output_rows.append(
                no_vendor_row(
                    asked_part_number,
                    asked_quantity,
                    "PART_NOT_FOUND",
                )
            )
            part_summaries.append(PartSummary(asked_part_number, "NOT_FOUND"))
            print(
                f"Input row {input_row_number}: {asked_part_number} | "
                f"Asked={decimal_to_string(asked_quantity)} | "
                f"PART_NOT_FOUND"
            )
            continue

        # Show the best-stocked vendor first.
        ordered_candidates = sorted(
            candidates,
            key=lambda candidate: (
                -candidate.available_quantity,
                candidate.vendor_name.lower(),
                candidate.row_number,
            ),
        )

        any_can_fulfill = False

        for candidate in ordered_candidates:
            can_fulfill = candidate.available_quantity >= asked_quantity
            any_can_fulfill = any_can_fulfill or can_fulfill

            row = {
                "customer_part_number": asked_part_number,
                "customer_quantity": decimal_to_string(asked_quantity),
                "vendor_name": candidate.vendor_name,
                "vendor_file": candidate.file_name,
                "vendor_available_quantity": decimal_to_string(
                    candidate.available_quantity
                ),
                "can_fulfill": "Yes" if can_fulfill else "No",
                "status": (
                    "CAN_FULFILL" if can_fulfill else "INSUFFICIENT_QUANTITY"
                ),
                "source_row_number": str(candidate.row_number),
            }
            row.update(blank_source_columns())

            for source_header in source_headers:
                row[f"source_{source_header}"] = (
                    candidate.original_data.get(source_header, "")
                )

            output_rows.append(row)

        part_summaries.append(
            PartSummary(
                asked_part_number,
                "MATCHED" if any_can_fulfill else "INSUFFICIENT",
            )
        )

        print(
            f"Input row {input_row_number}: {asked_part_number} | "
            f"Asked={decimal_to_string(asked_quantity)} | "
            f"{len(ordered_candidates)} vendor(s) found | "
            f"{'MATCHED' if any_can_fulfill else 'INSUFFICIENT_QUANTITY'}"
        )

    return output_rows, part_summaries


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def write_output(
    output_rows: list[dict[str, str]],
    source_headers: list[str],
) -> None:
    """
    Write the matching result to matching_output.csv.
    """
    output_headers = [
        "customer_part_number",
        "customer_quantity",
        "vendor_name",
        "vendor_file",
        "vendor_available_quantity",
        "can_fulfill",
        "status",
        "source_row_number",
    ]

    output_headers.extend(
        f"source_{header}" for header in source_headers
    )

    with OUTPUT_FILE.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=output_headers,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(output_rows)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        print("=" * 70)
        print("CSV ORDER MATCHING")
        print("=" * 70)

        scan_result = scan_raw_files()

        print()
        print(f"Unique part numbers indexed: {len(scan_result.part_index)}")

        input_rows, input_part_column, input_quantity_column = (
            load_input_file()
        )

        print(f"Input rows found: {len(input_rows)}")
        print(
            f"Input columns: part='{input_part_column}', "
            f"quantity='{input_quantity_column}'"
        )
        print()

        output_rows, part_summaries = match_orders(
            input_rows=input_rows,
            input_part_column=input_part_column,
            input_quantity_column=input_quantity_column,
            part_index=scan_result.part_index,
            source_headers=scan_result.source_headers,
        )

        write_output(output_rows, scan_result.source_headers)

        matched_parts = sum(
            1 for summary in part_summaries if summary.category == "MATCHED"
        )
        not_found_parts = sum(
            1
            for summary in part_summaries
            if summary.category == "NOT_FOUND"
        )
        insufficient_parts = sum(
            1
            for summary in part_summaries
            if summary.category == "INSUFFICIENT"
        )
        invalid_parts = sum(
            1 for summary in part_summaries if summary.category == "INVALID"
        )

        print()
        print("=" * 70)
        print("MATCH SUMMARY")
        print(f"Total customer parts            : {len(part_summaries)}")
        print(f"Matched parts                   : {matched_parts}")
        print(f"Parts not found                 : {not_found_parts}")
        print(f"Parts with insufficient quantity : {insufficient_parts}")
        if invalid_parts:
            print(f"Invalid input rows               : {invalid_parts}")
        print(f"Total vendors scanned            : {len(scan_result.vendor_names)}")
        print(f"Total vendor files processed     : {scan_result.processed_file_count}")
        print("=" * 70)
        print("COMPLETED")
        print(f"Total output rows : {len(output_rows)}")
        print(f"Output file       : {OUTPUT_FILE}")

        if scan_result.skipped_files:
            print()
            print("Skipped raw files:")
            for skipped_file in scan_result.skipped_files:
                print(f"  - {skipped_file}")

        print("=" * 70)

    except Exception as error:
        print()
        print("=" * 70)
        print("ERROR")
        print(str(error))
        print("=" * 70)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()