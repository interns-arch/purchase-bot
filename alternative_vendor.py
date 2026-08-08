"""For every Purchase Order line with Pending Qty > 0, find other vendors
currently stocking the same part and write
`output/alternative_vendor_report.xlsx`.

Computes gap analysis itself, so it can run independently of
`gap_analysis.py`:

    python alternative_vendor.py

Never recommends the vendor that already fell short on that line.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from core.db import get_session
from core.ingestion.column_detector import decimal_to_string
from core.logging_setup import get_logger
from core.services.alternative_vendor_service import find_all_alternatives
from core.services.gap_analysis_service import compute_gap_analysis

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_FILE = OUTPUT_DIR / "alternative_vendor_report.xlsx"

HEADERS = [
    "Part Number",
    "Part Num",
    "Part Description",
    "Current Stock",
    "Pending Qty",
    "Alternative Vendor",
    "Available Quantity",
    "Price",
    "Inventory File",
]

logger = get_logger("alternative_vendor")


def write_report(alternatives) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Alternative Vendors"

    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in alternatives:
        sheet.append(
            [
                row.part_number,
                decimal_to_string(row.pending_qty),
                row.alternative_vendor,
                decimal_to_string(row.available_quantity),
                decimal_to_string(row.price) if row.price is not None else "",
                row.inventory_file,
            ]
        )

    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value)) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = max_length + 2

    workbook.save(OUTPUT_FILE)


def main() -> None:
    logger.info("Searching for alternative vendors for pending PO lines ...")

    with get_session() as session:
        gap_rows = compute_gap_analysis(session)
        pending_rows = [row for row in gap_rows if row.pending_qty > 0]
        alternatives = find_all_alternatives(session)

    write_report(alternatives)

    parts_with_alternatives = {row.part_number for row in alternatives}
    parts_without_alternatives = [
        row.part_number for row in pending_rows if row.part_number not in parts_with_alternatives
    ]

    print("=" * 70)
    print("ALTERNATIVE VENDOR SEARCH")
    print(f"PO lines with pending qty       : {len(pending_rows)}")
    print(f"Alternative vendor matches found: {len(alternatives)}")
    print(f"Pending parts with NO alternative: {len(parts_without_alternatives)}")
    if parts_without_alternatives:
        print(f"  {', '.join(sorted(set(parts_without_alternatives)))}")
    print(f"Report written to               : {OUTPUT_FILE}")
    print("=" * 70)


if __name__ == "__main__":
    main()
