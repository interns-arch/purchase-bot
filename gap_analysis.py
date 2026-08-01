"""Compare Ordered Quantity vs Delivered Quantity for every Purchase Order
line and write `output/gap_report.xlsx`.

Run after `delivery_import.py`:

    python gap_analysis.py

Pending Quantity = Ordered Quantity - Delivered Quantity. Status is one of
FULLY DELIVERED / PARTIALLY DELIVERED / NOT DELIVERED.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
from openpyxl.styles import Font

from core.db import get_session
from core.ingestion.column_detector import decimal_to_string
from core.logging_setup import get_logger
from core.services.gap_analysis_service import (
    FULLY_DELIVERED,
    NOT_DELIVERED,
    PARTIALLY_DELIVERED,
    compute_gap_analysis,
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_FILE = OUTPUT_DIR / "gap_report.xlsx"

HEADERS = [
    "Vendor",
    "PO Number",
    "Part Number",
    "Ordered Qty",
    "Delivered Qty",
    "Pending Qty",
    "Status",
]

logger = get_logger("gap_analysis")


def write_gap_report(gap_rows) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Gap Report"

    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)

    for row in gap_rows:
        sheet.append(
            [
                row.vendor_name,
                row.po_number,
                row.part_number,
                decimal_to_string(row.ordered_qty),
                decimal_to_string(row.delivered_qty),
                decimal_to_string(row.pending_qty),
                row.status,
            ]
        )

    for column_cells in sheet.columns:
        max_length = max(len(str(cell.value)) for cell in column_cells)
        sheet.column_dimensions[column_cells[0].column_letter].width = max_length + 2

    workbook.save(OUTPUT_FILE)


def main() -> None:
    logger.info("Computing gap analysis (ordered vs delivered) ...")

    with get_session() as session:
        gap_rows = compute_gap_analysis(session)

    if not gap_rows:
        print("No purchase order items found. Run po_generator.py first.")
        return

    write_gap_report(gap_rows)

    fully = sum(1 for row in gap_rows if row.status == FULLY_DELIVERED)
    partial = sum(1 for row in gap_rows if row.status == PARTIALLY_DELIVERED)
    not_delivered = sum(1 for row in gap_rows if row.status == NOT_DELIVERED)
    total_pending = sum(row.pending_qty for row in gap_rows)

    print("=" * 70)
    print("GAP ANALYSIS")
    print(f"Total PO lines       : {len(gap_rows)}")
    print(f"Fully delivered      : {fully}")
    print(f"Partially delivered  : {partial}")
    print(f"Not delivered        : {not_delivered}")
    print(f"Total pending qty    : {decimal_to_string(total_pending)}")
    print(f"Report written to    : {OUTPUT_FILE}")
    print("=" * 70)


if __name__ == "__main__":
    main()
