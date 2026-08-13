"""One shared entry point for running the auto top-up after a vendor import.

Both upload paths use it -- the WhatsApp worker and the manual web upload's
background task -- so the behaviour (own session, own transaction, notify
only when something actually changed, never raise) can't drift between them.
"""

from __future__ import annotations

from backend.app.notifications import emitters as notifications
from core.db import get_session
from core.logging_setup import get_logger
from core.services.rules import topup

logger = get_logger(__name__)


def run_topup_for_vendor(vendor_id: int | None, vendor_name: str | None = None) -> None:
    """Fill recent customer-order shortfalls from this vendor's new stock.
    Adds only -- existing allocations are never moved. Must be called AFTER
    the import's transaction has committed."""
    if vendor_id is None:
        return
    try:
        with get_session() as session:
            result = topup.top_up_from_vendor(vendor_id, session)
        result.vendor_name = vendor_name
        if result.lines:
            logger.info(
                "Auto top-up from vendor %s filled %s line(s) across order(s) %s.",
                vendor_name or vendor_id,
                len(result.lines),
                result.order_ids,
            )
            notifications.publish_topup(result)
    except Exception:  # noqa: BLE001 -- top-up must never affect the import
        logger.exception("Auto top-up failed for vendor %s (import unaffected).", vendor_id)
