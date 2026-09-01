"""Delta rows -> the exact CSV bytes Dealer Portal's upload endpoint expects.

The contract (verified 2026-08-14, `STOCK_UPLOAD_API_GUIDE.md`):

    part_no,quantity,price,discount,tat_days

- The endpoint accepts CSV only. It does NOT accept a native `.xlsx`
  workbook, so ProcureHub converts.
- `quantity` is an INTEGER. ProcureHub stores `VendorInventory.quantity_available`
  as `Numeric(18,4)`, so it is FLOORED here -- never rounded up, which would
  promise stock the vendor does not have.
- `discount` is accepted by DP but never written to stock (known defect #1 in
  the guide). Always sent as 0 so nobody reads meaning into it later.
- Rows are already deduped by `delta.py`. This module asserts that rather
  than re-deduping, because DP SUMS duplicate rows for the same part and a
  duplicate slipping through would silently inflate the dealer's stock.
- `part_no` is ProcureHub's normalised part number (`normalise_part_number`
  -- `core/ingestion/column_detector.py:209`). No mapping table yet: shadow
  mode's `failed_count` tells us whether DP's parts master needs one.
  DP's CSV schema has no part-NAME column; the name comes from DP's own
  parts master, so `part_no` alone identifies the part.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from backend.app.integrations.dealer_portal.delta import DeltaRow
from core.logging_setup import get_logger

logger = get_logger(__name__)

CSV_COLUMNS = ("part_no", "quantity", "price", "discount", "tat_days")


def _quantity(value) -> int:
    """Floor to a whole number. Negative quantities cannot reach DP."""
    if value is None:
        return 0
    quantity = int(Decimal(value).to_integral_value(rounding="ROUND_FLOOR"))
    return max(quantity, 0)


def _price(value) -> str:
    if value is None:
        return "0"
    # Two decimal places, no scientific notation, no trailing noise.
    return f"{Decimal(value):.2f}"


def build_csv(rows: list[DeltaRow], *, tat_days: int) -> bytes:
    """UTF-8 CSV bytes for `rows`, ready to post as the multipart `file`
    field. Returns b"" for an empty row list -- the caller must not upload
    an empty CSV."""
    if not rows:
        return b""

    seen: set[str] = set()
    duplicates: list[str] = []

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)

    for row in rows:
        part_no = (row.part_no or "").strip()
        if not part_no:
            continue
        if part_no in seen:
            # delta.py should have collapsed these already. If one reaches
            # here it MUST be dropped, not written: DP would sum it.
            duplicates.append(part_no)
            continue
        seen.add(part_no)
        writer.writerow(
            [
                part_no,
                _quantity(row.quantity),
                _price(row.price),
                0,  # discount -- accepted by DP, never written (known defect)
                tat_days,
            ]
        )

    if duplicates:
        logger.error(
            "Dealer Portal CSV: dropped %d duplicate part row(s) (%s%s). DP sums "
            "duplicates, so writing them would have inflated the dealer's stock.",
            len(duplicates),
            ", ".join(duplicates[:5]),
            "..." if len(duplicates) > 5 else "",
        )

    return buffer.getvalue().encode("utf-8")


def preview(rows: list[DeltaRow], *, tat_days: int, limit: int = 10) -> str:
    """A short human-readable extract of the CSV, for shadow-mode logging.
    Never logs the whole file -- a 500-part vendor would flood the log."""
    body = build_csv(rows, tat_days=tat_days).decode("utf-8").splitlines()
    if not body:
        return "(no rows)"
    head = body[: limit + 1]
    if len(body) > len(head):
        head.append(f"... {len(body) - len(head)} more row(s)")
    return "\n".join(head)
