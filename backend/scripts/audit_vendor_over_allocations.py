"""Read-only, one-time reconciliation report: finds any (vendor, part) pair
where the sum of every `VendorSelection` already made against it exceeds
that vendor's currently-imported quantity.

This can only happen from data created BEFORE the shared vendor-stock
reservation ledger fix (`core/services/vendor_stock_service.py`,
`vendor_selection_service.upsert_selection`'s cross-order remaining-stock
check) -- any `VendorSelection` created after that fix shipped is guaranteed
not to over-commit. Existing over-committed rows are NOT modified here: a
Purchase Order may already have been generated and emailed from one of them,
so silently rewriting that history would be its own kind of harm. This is
deliberately just a report for the purchase team to review by hand.

Run from the repository root:

    python -m backend.scripts.audit_vendor_over_allocations
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

# Same reasoning as migrate_schema_updates.py: load backend/.env BEFORE
# importing core.db, so a standalone run targets the same database the web
# app does.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.db import engine, init_db  # noqa: E402
from core.models import InventoryImport, Part, Vendor, VendorInventory, VendorSelection  # noqa: E402


def find_over_allocations(session: Session) -> list[dict]:
    reserved_by_pair = {
        (vendor_id, part_id): reserved
        for vendor_id, part_id, reserved in session.execute(
            select(
                VendorSelection.vendor_id,
                VendorSelection.part_id,
                func.sum(VendorSelection.quantity_selected),
            ).group_by(VendorSelection.vendor_id, VendorSelection.part_id)
        ).all()
    }

    findings = []
    for (vendor_id, part_id), reserved in reserved_by_pair.items():
        raw_row = session.execute(
            select(VendorInventory)
            .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
            .where(
                VendorInventory.vendor_id == vendor_id,
                VendorInventory.part_id == part_id,
                InventoryImport.is_active.is_(True),
            )
        ).scalar_one_or_none()
        raw_available = raw_row.quantity_available if raw_row is not None else 0

        if reserved > raw_available:
            vendor = session.get(Vendor, vendor_id)
            part = session.get(Part, part_id)
            findings.append(
                {
                    "vendor_id": vendor_id,
                    "vendor_name": vendor.name if vendor else f"#{vendor_id}",
                    "part_id": part_id,
                    "part_number": part.canonical_part_number if part else f"#{part_id}",
                    "raw_available": raw_available,
                    "total_reserved": reserved,
                    "over_by": reserved - raw_available,
                }
            )

    findings.sort(key=lambda f: f["over_by"], reverse=True)
    return findings


def main() -> int:
    init_db()
    with Session(engine) as session:
        print(f"Target database: {engine.url.render_as_string(hide_password=True)}")
        findings = find_over_allocations(session)

        if not findings:
            print("No over-allocations found -- every vendor+part's selections are within stock.")
            return 0

        print(f"\n{len(findings)} vendor+part pair(s) with total selections exceeding imported stock:\n")
        for f in findings:
            print(
                f"  Vendor '{f['vendor_name']}' (id={f['vendor_id']}) / "
                f"Part '{f['part_number']}' (id={f['part_id']}): "
                f"raw available={f['raw_available']}, total reserved={f['total_reserved']} "
                f"(over by {f['over_by']})"
            )
        print(
            "\nThese rows were NOT modified -- review manually (a Purchase Order may already "
            "have been generated from some of them). Consider contacting the affected "
            "customers/vendors before adjusting any VendorSelection/VendorPurchaseOrder rows."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
