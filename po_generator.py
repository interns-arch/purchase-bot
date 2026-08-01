"""Generate vendor Purchase Orders from a chosen-vendor matching run.

PAUSED as of 2026-07-31: `order_matching.py` was redesigned to only search
vendor inventory and write a Vendor Comparison Report (every vendor listed,
none chosen) -- it no longer writes `output/matching_output.csv` or picks a
vendor per line. This script still expects that legacy file, produced by a
future Vendor Selection module (manual or rule-based) that hasn't been
built yet. Until that module exists and writes a compatible
`matching_output.csv`, this script has no valid input to run against; it is
no longer part of `run_pipeline.py`'s default sequence (see its comments).

Prerequisite bridge between the (future) Vendor Selection phase and the
Vendor Delivery workflow below: order matching only writes a flat
`output/matching_output.csv`; it never persists a real Purchase Order to the
database. `delivery_import.py` and everything after it need a real PO
Number + Ordered Quantity to validate deliveries against, so this script
turns each vendor's MATCHED/PARTIAL lines into a `PurchaseOrder` (one per
vendor per matching run).

Once Vendor Selection exists, run this after it and before
`delivery_import.py`:

    python po_generator.py
    python delivery_import.py

Re-running against an unchanged matching_output.csv is a no-op.
"""

from __future__ import annotations

from pathlib import Path

from core.db import get_session
from core.logging_setup import get_logger
from core.services.purchase_order_service import (
    MatchingOutputNotFoundError,
    generate_purchase_orders,
)

BASE_DIR = Path(__file__).resolve().parent
MATCHING_OUTPUT_FILE = BASE_DIR / "output" / "matching_output.csv"

logger = get_logger("po_generator")


def main() -> None:
    logger.info("Generating purchase orders from %s", MATCHING_OUTPUT_FILE)

    with get_session() as session:
        try:
            result = generate_purchase_orders(MATCHING_OUTPUT_FILE, session)
        except MatchingOutputNotFoundError:
            logger.error(
                "%s not found. Run order_matching.py first.", MATCHING_OUTPUT_FILE
            )
            raise SystemExit(1)

    print("=" * 70)
    if result.already_generated:
        print("Purchase orders were already generated for this matching_output.csv.")
        print("Re-run order_matching.py to produce a new matching result first.")
        print("=" * 70)
        return

    print("PURCHASE ORDER GENERATION COMPLETE")
    print(f"Purchase orders created : {len(result.purchase_orders_created)}")
    for po_number in result.purchase_orders_created:
        print(f"  - {po_number}")
    print(f"PO line items created   : {result.items_created}")
    print(f"Rows skipped            : {result.skipped_rows}")

    if result.skip_reasons:
        logger.warning("%d row(s) skipped:", len(result.skip_reasons))
        for reason in result.skip_reasons:
            logger.warning("  %s", reason)

    print("=" * 70)


if __name__ == "__main__":
    main()
