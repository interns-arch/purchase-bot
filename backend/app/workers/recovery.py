"""Startup crash recovery for the allocation queue.

The automatic-allocation batch queue lives in memory (debounce timers, see
`allocation_batch`). A crash -- e.g. the 17-Aug OOM restart on Render's
512MB instance -- loses whatever was queued: a customer order committed just
before the crash would silently never be allocated, and nobody would know.

On every startup (a little after boot, so the app is settled), this scans
the last 24 hours for orders that:

  1. have at least one line,
  2. have ZERO `VendorSelection` rows (never allocated), and
  3. have at least one line matching ACTIVE vendor stock (alias-aware --
     the same `_matchable_part_numbers` the allocation engine uses),

and re-queues them through the NORMAL batch -- allocation, report workbook
and notifications then run exactly as they would have originally. Orders
that genuinely match nothing are left alone (re-running them would just
produce a noisy "no reports" warning on every restart), and orders that
already have any allocation are never touched.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from backend.app.integrations.whatsapp import allocation_batch
from core.db import get_session
from core.ingestion.column_detector import normalise_part_number
from core.logging_setup import get_logger
from core.models import (
    CustomerOrder,
    CustomerOrderItem,
    InventoryImport,
    VendorInventory,
    VendorSelection,
)
from core.services.vendor_selection_service import _matchable_part_numbers

logger = get_logger(__name__)

RECOVERY_WINDOW_HOURS = 24


def _has_matchable_stock(items, session) -> bool:
    for item in items:
        numbers = _matchable_part_numbers(
            normalise_part_number(item.part_number_raw), session
        )
        count = session.execute(
            select(func.count())
            .select_from(VendorInventory)
            .join(InventoryImport, VendorInventory.import_id == InventoryImport.id)
            .where(
                VendorInventory.normalized_part_number.in_(numbers),
                InventoryImport.is_active.is_(True),
            )
        ).scalar_one()
        if count:
            return True
    return False


def requeue_unallocated_recent_orders() -> None:
    """Find and re-queue crash-lost orders. Never raises (scheduler job)."""
    try:
        to_queue: list[int] = []
        with get_session() as session:
            cutoff = datetime.utcnow() - timedelta(hours=RECOVERY_WINDOW_HOURS)
            orders = session.execute(
                select(CustomerOrder)
                .where(CustomerOrder.created_at >= cutoff)
                .order_by(CustomerOrder.id)
            ).scalars().all()
            for order in orders:
                items = session.execute(
                    select(CustomerOrderItem).where(
                        CustomerOrderItem.customer_order_id == order.id
                    )
                ).scalars().all()
                if not items:
                    continue
                selection_count = session.execute(
                    select(func.count())
                    .select_from(VendorSelection)
                    .join(
                        CustomerOrderItem,
                        VendorSelection.customer_order_item_id == CustomerOrderItem.id,
                    )
                    .where(CustomerOrderItem.customer_order_id == order.id)
                ).scalar_one()
                if selection_count:
                    continue  # already allocated (even partially) -- never touch
                if _has_matchable_stock(items, session):
                    to_queue.append(order.id)

        if not to_queue:
            logger.info("Allocation recovery: nothing to re-queue.")
            return
        logger.info(
            "Allocation recovery: re-queuing %d order(s) with no allocation: %s "
            "(likely lost to a crash/restart).",
            len(to_queue),
            to_queue,
        )
        for order_id in to_queue:
            allocation_batch.request_order_allocation(order_id)
    except Exception:  # noqa: BLE001 -- recovery must never break startup
        logger.exception("Allocation recovery scan failed (startup unaffected).")
