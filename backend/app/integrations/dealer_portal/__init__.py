"""Dealer Portal stock push.

Dealer Portal (DP) is becoming the single centralized repository for all
vendor stock -- it is what Odoo, the accounts team, the sales mobile app and
Shopify read from. ProcureHub keeps its WhatsApp intake and its parsing; what
changes is that the parsed result is ALSO pushed into DP under each vendor's
own DP dealer account.

ProcureHub's database stays, not as the stock repository but as the DIFF
BUFFER and AUDIT LOG: DP leaves parts absent from an upload unchanged
forever, so somebody has to know which parts sold out and zero them
explicitly. Only ProcureHub holds yesterday's list. See `delta.py`.

The public entry point is `request_push(vendor_id)` -- best-effort, never
raises, and a no-op unless `DEALER_PORTAL_ENABLED=true`.
"""

from backend.app.integrations.dealer_portal.push_service import (
    push_account,
    request_push,
    retry_failed_pushes,
)

__all__ = ["request_push", "push_account", "retry_failed_pushes"]
