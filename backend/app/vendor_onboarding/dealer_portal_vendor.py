"""Dealer Portal calls for vendor creation and ledger upload.

The portal's vendor/ledger API details are not available yet, so this runs in
DRY-RUN by default: the exact payload is logged and a "DRY-RUN" reference is
returned, and every other step (approval, ProcureHub vendor, WhatsApp
messages) works for real. Once the API is known, set the URLs + key and
DEALER_PORTAL_VENDOR_API_DRY_RUN=false -- and adjust `_vendor_payload` /
`_ledger_payload` to the portal's field names if they differ."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx

from core.logging_setup import get_logger

logger = get_logger(__name__)


class _Settings:
    vendor_url: str = (os.environ.get("DEALER_PORTAL_VENDOR_API_URL") or "").strip()
    ledger_url: str = (os.environ.get("DEALER_PORTAL_LEDGER_API_URL") or "").strip()
    api_key: str = (os.environ.get("DEALER_PORTAL_VENDOR_API_KEY") or "").strip()
    dry_run: bool = os.environ.get("DEALER_PORTAL_VENDOR_API_DRY_RUN", "true").strip().lower() != "false"
    timeout: float = float(os.environ.get("DEALER_PORTAL_VENDOR_API_TIMEOUT", "30") or 30)


settings = _Settings()


@dataclass(frozen=True)
class PortalResult:
    ok: bool
    reference: str | None = None
    error: str | None = None


def _vendor_payload(answers: dict, *, vendor_code: str | None, gst: dict | None) -> dict:
    return {
        "vendor_code": vendor_code,
        "name": answers.get("vendor_name"),
        "gstin": answers.get("gstin"),
        "pan": answers.get("pan"),
        "vendor_type": answers.get("vendor_type"),
        "brands_supplied": answers.get("brands_supplied"),
        "contact_person": answers.get("contact_person"),
        "mobile": answers.get("mobile"),
        "email": answers.get("email"),
        "address": answers.get("address"),
        "bank": {
            "account_holder": answers.get("account_holder"),
            "bank_name": answers.get("bank_name"),
            "account_number": answers.get("account_number"),
            "ifsc": answers.get("ifsc"),
        },
        "brand_discounts": answers.get("brand_discounts") or [],
        "credit_days": answers.get("credit_days"),
        "credit_limit": answers.get("credit_limit"),
        "dispatch_mode": answers.get("dispatch_mode"),
        "freight_terms": answers.get("freight_terms"),
        "transporter": answers.get("transporter"),
        "lead_time_days": answers.get("lead_time_days"),
        "min_order_value": answers.get("min_order_value"),
        "return_policy": answers.get("return_policy"),
        "remarks": answers.get("remarks"),
        "gst_legal_name": (gst or {}).get("legal_name"),
    }


def _post(url: str, payload: dict, what: str) -> PortalResult:
    if settings.dry_run or not url:
        logger.info(
            "Dealer Portal %s DRY-RUN (no API configured) -- payload: %s",
            what,
            json.dumps(payload, default=str)[:4000],
        )
        return PortalResult(ok=True, reference="DRY-RUN")
    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {settings.api_key}", "x-api-key": settings.api_key},
            timeout=settings.timeout,
        )
    except Exception as exc:  # noqa: BLE001 -- reported to the approver, retried from the UI
        logger.exception("Dealer Portal %s call failed.", what)
        return PortalResult(ok=False, error=f"Dealer Portal unreachable: {exc.__class__.__name__}")
    body = {}
    try:
        body = response.json()
    except ValueError:
        pass
    if response.status_code >= 400 or (isinstance(body, dict) and body.get("success") is False):
        detail = (body.get("message") or body.get("error")) if isinstance(body, dict) else None
        error = f"Dealer Portal HTTP {response.status_code}: {detail or response.text[:200]}"
        logger.warning("Dealer Portal %s rejected: %s", what, error)
        return PortalResult(ok=False, error=error)
    reference = None
    if isinstance(body, dict):
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        reference = str(data.get("id") or data.get("vendor_id") or data.get("reference") or "") or None
    logger.info("Dealer Portal %s succeeded (ref=%s).", what, reference)
    return PortalResult(ok=True, reference=reference or "OK")


def create_vendor(answers: dict, *, vendor_code: str | None, gst: dict | None) -> PortalResult:
    return _post(settings.vendor_url, _vendor_payload(answers, vendor_code=vendor_code, gst=gst), "vendor create")


def upload_ledger(submission, *, vendor_code: str | None, dealer_portal_vendor_ref: str | None) -> PortalResult:
    payload = {
        "vendor_code": vendor_code,
        "dealer_portal_vendor_id": dealer_portal_vendor_ref,
        "period_from": str(submission.period_from) if submission.period_from else None,
        "period_to": str(submission.period_to) if submission.period_to else None,
        "opening_balance": str(submission.opening_balance) if submission.opening_balance is not None else None,
        "opening_side": submission.opening_side,
        "closing_balance": str(submission.closing_balance) if submission.closing_balance is not None else None,
        "closing_side": submission.closing_side,
        "debit_total": str(submission.debit_total) if submission.debit_total is not None else None,
        "credit_total": str(submission.credit_total) if submission.credit_total is not None else None,
        "lines": submission.lines or [],
        "checked_by": submission.decided_by,
    }
    return _post(settings.ledger_url, payload, "ledger upload")
