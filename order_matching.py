"""Inventory Search: search every vendor's active inventory for a customer
order (input.csv) and generate a Vendor Comparison Report.

Mirrors the real business step "customer places input.csv": run this after
inventory_import.py has loaded the vendors' active inventory into the
database.

    python order_matching.py

Redesigned workflow (2026-07-31): this step only SEARCHES and LISTS. It does
NOT choose a vendor.

    Customer Order
        |
    Search All Vendor Inventories        <- this script
        |
    Generate Vendor Comparison Report    <- output/vendor_comparison_report.xlsx
        |
    (Manual or Automatic Vendor Selection -- future module, not built yet)
        |
    Generate Purchase Order
        |
    Vendor Delivery Upload
        |
    Compare Ordered vs Delivered Quantity
        |
    Gap Analysis
        |
    Vendor Performance Dashboard

For every customer order line, every vendor that stocks the part is listed
as its own row (best-stocked vendor first) with:
    Available     vendor's available quantity >= requested quantity
    Partial       vendor has some stock, but less than requested
    Out of Stock  vendor carries the part but currently has none
    Not Found     no vendor in the system carries the part at all

Vendor Selection (manual, or a business rule such as lowest MRP, lowest sale
price, highest available quantity, vendor performance score, or a
combination of these) is intentionally a separate, not-yet-built module.
Keeping this step to "search and list only" means that future selection
logic can be added without ever touching this file or
vendor_comparison_service.py.

NOTE ON THE REST OF THE PIPELINE: po_generator.py (and everything after it)
turns a *chosen* vendor per line into a real Purchase Order. Since vendor
selection doesn't exist yet, that step has no valid input right now --
run_pipeline.py stops after this script until the Vendor Selection module is
built (see run_pipeline.py's comments).

This script only reads inventory; it does not write anything back to the
database.
"""

from __future__ import annotations

from pathlib import Path

from core.db import get_session
from core.ingestion.column_detector import find_required_columns
from core.ingestion.csv_reader import read_csv_rows
from core.services.vendor_comparison_service import (
    VendorComparisonRow,
    compare_vendors,
    to_workbook,
)

BASE_DIR = Path(__file__).resolve().parent
INPUT_FILE = BASE_DIR / "input.csv"
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_FILE = OUTPUT_DIR / "vendor_comparison_report.xlsx"


def write_report(rows: list[VendorComparisonRow]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    to_workbook(rows).save(OUTPUT_FILE)


def main() -> None:
    if not INPUT_FILE.exists():
        print(f"Input file not found: {INPUT_FILE}")
        raise SystemExit(1)

    parsed_file = read_csv_rows(INPUT_FILE)
    part_column, quantity_column = find_required_columns(parsed_file.headers, INPUT_FILE.name)

    with get_session() as session:
        result = compare_vendors(parsed_file.rows, part_column, quantity_column, session)

    write_report(result.rows)

    summary = result.summary

    print("=" * 70)
    print("VENDOR SEARCH COMPLETED")
    print("=" * 70)
    print()
    print(f"Customer Order Items   : {summary.customer_order_items}")
    print()
    print(f"Matched Items          : {summary.matched_items}")
    print(f"Not Found              : {summary.not_found_items}")
    if summary.invalid_items:
        print(f"Invalid Order Lines    : {summary.invalid_items}")
    print()
    print(f"Matching Vendors Found : {summary.matching_vendors_found}")
    print()
    print("Output Files:")
    print(f"  - {OUTPUT_FILE.relative_to(BASE_DIR)}")
    print()
    print("=" * 70)


if __name__ == "__main__":
    main()
