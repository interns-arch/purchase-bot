"""Scan delivery_files/ and import every vendor delivery file found there.

Mirrors the real business step "vendor/warehouse staff uploads a delivery
file after dispatch": run this whenever new delivery files land in that
folder, once Purchase Orders exist (`po_generator.py`).

    python delivery_import.py

Each row is validated against Vendor / PO Number / Part Number / Delivered
Quantity and matched to the Purchase Order it fulfils. Valid rows are stored
as `DeliveryItem` records; invalid rows are logged to `delivery_import_errors`
and skipped -- one bad row never stops the rest of the file. Re-importing an
unchanged file is skipped (duplicate detection by content hash).
"""

from __future__ import annotations

from pathlib import Path

from core.db import get_session
from core.logging_setup import get_logger
from core.models import DeliveryImportStatus
from core.services import delivery_import_service as import_service

BASE_DIR = Path(__file__).resolve().parent
DELIVERY_FILES_DIR = BASE_DIR / "delivery_files"
SUPPORTED_EXTENSIONS = import_service.SUPPORTED_EXTENSIONS

logger = get_logger("delivery_import")


def main() -> None:
    if not DELIVERY_FILES_DIR.exists():
        logger.error("delivery_files/ directory not found at %s", DELIVERY_FILES_DIR)
        raise SystemExit(1)

    files = sorted(
        file_path
        for file_path in DELIVERY_FILES_DIR.iterdir()
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not files:
        print(f"No delivery files found in {DELIVERY_FILES_DIR}")
        return

    print(f"Found {len(files)} delivery file(s) in delivery_files/")
    print("=" * 70)

    files_imported = 0
    files_skipped = 0
    files_failed = 0
    total_rows = 0
    total_errors = 0

    for file_path in files:
        print(f"\n{file_path.name}")

        with get_session() as session:
            try:
                result = import_service.run_delivery_import(file_path, session)
            except import_service.DuplicateDeliveryFileError as error:
                files_skipped += 1
                print(f"  SKIPPED: {error}")
                continue
            except (ValueError, FileNotFoundError) as error:
                files_failed += 1
                logger.error("Failed to import %s: %s", file_path.name, error)
                continue

        total_rows += result.row_count
        total_errors += result.error_count
        if result.status == DeliveryImportStatus.FAILED:
            files_failed += 1
        else:
            files_imported += 1

        print(
            f"  Import #{result.import_id}: {result.status.value} "
            f"({result.row_count} rows stored, {result.error_count} errors)"
        )
        for message in result.errors:
            logger.warning("  %s", message)

    print("\n" + "=" * 70)
    print("DELIVERY IMPORT COMPLETE")
    print(f"Files imported     : {files_imported}")
    print(f"Files skipped (dup): {files_skipped}")
    print(f"Files failed       : {files_failed}")
    print(f"Delivery rows saved: {total_rows}")
    print(f"Row-level errors   : {total_errors}")
    print("=" * 70)


if __name__ == "__main__":
    main()
