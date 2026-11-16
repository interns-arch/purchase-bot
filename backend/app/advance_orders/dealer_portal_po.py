"""Dealer Portal PURCHASE ORDER + TRANSIT when a customer confirms an advance
order through the sales bot (Founder, 10 Oct 2026).

One PO per vendor (the parts that vendor said he has), then a transit entry
for it. The portal's references are kept on the order (`AdvanceOrder.
dealer_portal`), shown to the team, and returned to the sales bot in the
order JSON.

The portal's PO / transit API details are not available yet, so this runs in
DRY-RUN by default: the exact payload is logged and "DRY-RUN" is stored as
the reference; the confirmation itself (vendor told, team told) is never held
up. When the API arrives: set DEALER_PORTAL_PO_API_URL,
DEALER_PORTAL_TRANSIT_API_URL, DEALER_PORTAL_PO_API_KEY and
DEALER_PORTAL_PO_DRY_RUN=false, and adjust `_po_payload` / `_transit_payload`
to the portal's field names."""

from __future__ import annotations

import json
import os

import httpx

from core.logging_setup import get_logger

logger = get_logger(__name__)


class _Settings:
    po_url: str = (os.environ.get("DEALER_PORTAL_PO_API_URL") or "").strip()
    transit_url: str = (os.environ.get("DEALER_PORTAL_TRANSIT_API_URL") or "").strip()
    api_key: str = (os.environ.get("DEALER_PORTAL_PO_API_KEY") or "").strip()
    dry_run: bool = os.environ.get("DEALER_PORTAL_PO_DRY_RUN", "true").strip().lower() != "false"
    timeout: float = float(os.environ.get("DEALER_PORTAL_PO_API_TIMEOUT", "30") or 30)


settings = _Settings()


def _money(value) -> str | None:
    return None if value is None else str(value)


def _post(url: str, payload: dict, what: str) -> dict:
    """{"ok": bool, "reference": str|None, "error": str|None}. Never raises."""
    if settings.dry_run or not url:
        logger.info("Dealer Portal %s DRY-RUN -- payload: %s", what, json.dumps(payload, default=str)[:4000])
        return {"ok": True, "reference": "DRY-RUN", "error": None}
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {settings.api_key}", "x-api-key": settings.api_key},
            timeout=settings.timeout,
        )
        body = response.json() if response.content else {}
    except Exception as exc:  # noqa: BLE001 -- reported, never blocks the confirmation
        logger.exception("Dealer Portal %s failed.", what)
        return {"ok": False, "reference": None, "error": f"unreachable: {exc.__class__.__name__}"}
    if response.status_code >= 400 or (isinstance(body, dict) and body.get("success") is False):
        detail = (body.get("message") or body.get("error")) if isinstance(body, dict) else None
        return {"ok": False, "reference": None, "error": f"HTTP {response.status_code}: {detail or response.text[:200]}"}
    data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body
    reference = None
    if isinstance(data, dict):
        reference = str(data.get("po_number") or data.get("transit_id") or data.get("id") or data.get("reference") or "") or None
    logger.info("Dealer Portal %s created (ref=%s).", what, reference)
    return {"ok": True, "reference": reference or "OK", "error": None}


def _po_payload(order, vendor, lines) -> dict:
    return {
        "external_ref": f"ADV-{order.id}",
        "sales_ref": order.external_ref,
        "vendor_code": getattr(vendor, "vendor_code", None),
        "vendor_name": getattr(vendor, "name", None),
        "customer": {
            "name": order.customer_name,
            "phone": order.customer_phone,
            "portal_id": order.customer_portal_id,
        },
        "needed_by": str(order.needed_by) if order.needed_by else None,
        "lines": [
            {
                "part_number": line.part_number,
                "part_name": line.part_name,
                "brand": line.brand,
                "qty": line.available_qty or line.qty,
                "eta": str(line.eta_date) if line.eta_date else None,
                "mrp": _money(line.mrp),
                "discount_pct": _money(line.discount_pct),
                "net_price": _money(line.net_price),
            }
            for line in lines
        ],
    }


def _transit_payload(order, vendor, lines, po_reference: str | None) -> dict:
    etas = [line.eta_date for line in lines if line.eta_date]
    return {
        "po_reference": po_reference,
        "external_ref": f"ADV-{order.id}",
        "vendor_code": getattr(vendor, "vendor_code", None),
        "expected_date": str(max(etas)) if etas else None,
        "lines": [
            {"part_number": line.part_number, "qty": line.available_qty or line.qty,
             "eta": str(line.eta_date) if line.eta_date else None}
            for line in lines
        ],
    }


def create_po_and_transit(order, vendor, lines) -> dict:
    """Create the PO, then its transit entry. Returns what to store on the
    order: {"po": ..., "transit": ..., "error": ...}."""
    po = _post(settings.po_url, _po_payload(order, vendor, lines), f"purchase order ADV-{order.id}")
    result = {"po": po.get("reference"), "transit": None, "error": po.get("error")}
    if po.get("ok"):
        transit = _post(
            settings.transit_url, _transit_payload(order, vendor, lines, po.get("reference")), f"transit ADV-{order.id}"
        )
        result["transit"] = transit.get("reference")
        result["error"] = transit.get("error")
    return result
