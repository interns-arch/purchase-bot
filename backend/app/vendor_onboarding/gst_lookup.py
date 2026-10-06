"""GSTIN lookup against the GST registry API (gstinapi.in).

The account is PAID PER CALL, so: the offline checksum must pass first, each
GSTIN is looked up at most once per process (and the result is stored on the
request row by the caller), and any failure simply means "not verified
online" -- it never blocks a vendor."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from backend.app.vendor_onboarding.config import vendor_onboarding_settings
from core.logging_setup import get_logger

logger = get_logger(__name__)

_cache: dict[str, "GstRecord | None"] = {}


@dataclass(frozen=True)
class GstRecord:
    gstin: str
    legal_name: str | None
    trade_name: str | None
    status: str | None  # "Active", "Cancelled", ...
    address: str | None
    pincode: str | None
    raw: dict

    @property
    def is_active(self) -> bool:
        return (self.status or "").strip().lower() == "active"

    @property
    def display_name(self) -> str | None:
        return self.trade_name or self.legal_name


def lookup(gstin: str) -> GstRecord | None:
    """The registry record, or None when not configured / not found / the
    API failed. Never raises."""
    settings = vendor_onboarding_settings
    if not settings.gst_api_key:
        return None
    if gstin in _cache:
        return _cache[gstin]
    record: GstRecord | None = None
    try:
        response = httpx.get(
            f"{settings.gst_api_url}/{gstin}",
            headers={
                "x-api-key": settings.gst_api_key,
                "Authorization": f"Bearer {settings.gst_api_key}",
            },
            timeout=20.0,
        )
        body = response.json() if response.content else {}
        data = body.get("data") if isinstance(body, dict) else None
        if response.status_code == 200 and body.get("success") and isinstance(data, dict):
            address = data.get("address")
            pincode = data.get("pincode")
            if address and pincode and pincode not in address:
                address = f"{address} - {pincode}"
            record = GstRecord(
                gstin=gstin,
                legal_name=data.get("legal_name"),
                trade_name=data.get("trade_name"),
                status=data.get("status"),
                address=address,
                pincode=pincode,
                raw=data,
            )
            logger.info(
                "GST lookup %s: %s (%s); credits remaining %s.",
                gstin,
                record.display_name,
                record.status,
                body.get("credits_remaining"),
            )
        else:
            logger.warning(
                "GST lookup %s returned HTTP %s: %s",
                gstin,
                response.status_code,
                str(body)[:200],
            )
    except Exception:  # noqa: BLE001 -- verification is best-effort
        logger.exception("GST lookup failed for %s.", gstin)
        return None  # not cached: a network blip may succeed next time
    _cache[gstin] = record
    return record
