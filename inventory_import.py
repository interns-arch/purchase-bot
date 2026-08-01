"""Scan raw_files/ and import every vendor inventory file found there.

This mirrors the real business step "vendor drops inventory files into
raw_files/": run this script whenever new or updated vendor files land in
that folder.

    python inventory_import.py

Vendor identity is derived from the file name (its stem), auto-creating the
vendor on first sight. Re-running with an unchanged file is a no-op -- the
duplicate is detected and the batch is skipped rather than piling up
identical history. A changed file becomes a new active import batch while
the previous one is kept as history.
"""

from __future__ import annotations

from pathlib import Path

from core.db import get_session
from core.models import ImportStatus
from core.services import inventory_import_service as import_service
from core.services import vendor_service

BASE_DIR = Path(__file__).resolve().parent
RAW_FILES_DIR = BASE_DIR / "raw_files"
SUPPORTED_EXTENSIONS = import_service.SUPPORTED_EXTENSIONS


def _vendor_name_from_file(file_path: Path) -> str:
    return file_path.stem.strip()


def _get_or_create_vendor(name, session):
    vendor = vendor_service.get_vendor_by_name(name, session)
    if vendor is None:
        vendor = vendor_service.create_vendor(name, session)
        print(f"  New vendor created: '{vendor.name}' (id={vendor.id})")
    return vendor


def main() -> None:
    if not RAW_FILES_DIR.exists():
        print(f"raw_files/ directory not found at {RAW_FILES_DIR}")
        raise SystemExit(1)

    files = sorted(
        file_path
        for file_path in RAW_FILES_DIR.iterdir()
        if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not files:
        print(f"No vendor files found in {RAW_FILES_DIR}")
        return

    print(f"Found {len(files)} vendor file(s) in raw_files/")
    print("=" * 70)

    for file_path in files:
        vendor_name = _vendor_name_from_file(file_path)
        print(f"\n{file_path.name} -> vendor '{vendor_name}'")

        with get_session() as session:
            vendor = _get_or_create_vendor(vendor_name, session)

            try:
                result = import_service.run_import(vendor.id, file_path, session)
            except Exception as error:
                print(f"  FAILED: {error}")
                continue

            if result.status == ImportStatus.AWAITING_CONFIRMATION:
                import_service.cancel_import(result.import_id, session)
                print(
                    f"  Unchanged since import #{result.duplicate_of_import_id}, skipped."
                )
                continue

            print(
                f"  Import #{result.import_id}: {result.status.value} "
                f"({result.row_count} rows, {result.error_count} errors)"
            )

    print("\n" + "=" * 70)
    print("Import scan complete.")


if __name__ == "__main__":
    main()
