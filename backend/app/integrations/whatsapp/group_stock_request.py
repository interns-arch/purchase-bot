"""The daily "stock share kar dijiye" in every vendor WhatsApp group
(29 Oct 2026). Posted at WHAPI_STOCK_REQUEST_TIMES (09:30 and 18:00 IST) from
the group number (Alam sir's own WhatsApp), worded the way he would say it.
Whatever stock the vendor shares back is taken by the quiet vendor-group
handling in `whapi` and uploaded like any other stock file.

A vendor who sent stock in the last WHAPI_STOCK_REQUEST_SKIP_HOURS (2) is not
asked again. Groups are posted one by one with a pause between them, so 40+
groups never go out as one burst from a personal number.

Runs even in purchase approval mode: this is the one message the team asked
the bot to send vendors itself."""

from __future__ import annotations

import os
import random
import time
from datetime import timedelta

from sqlalchemy import func, select

from core.logging_setup import get_logger

logger = get_logger(__name__)

# The team's own words (29 Oct 2026), same at 09:30 and 18:00.
MORNING = ["Namaste ji 🙏 aaj ka stock share kar dijiye, main order bana leta hoon."]
EVENING = MORNING


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip()


def request_times() -> list[str]:
    return [t.strip() for t in _env("WHAPI_STOCK_REQUEST_TIMES", "09:30,18:00").split(",") if t.strip()]


def enabled() -> bool:
    from backend.app.integrations.whatsapp import whapi

    return _env("WHAPI_STOCK_REQUEST_ENABLED", "false").lower() == "true" and whapi.enabled()


def _recent_senders(hours: float) -> set[str]:
    """Vendor codes with stock uploaded in the last `hours`."""
    from core.db import get_session
    from core.models import InventoryImport, Vendor
    from core.time_utils import now_ist_naive

    since = now_ist_naive() - timedelta(hours=hours)
    with get_session() as session:
        rows = session.execute(
            select(func.upper(Vendor.vendor_code))
            .join(InventoryImport, InventoryImport.vendor_id == Vendor.id)
            .where(InventoryImport.created_at >= since)
            .distinct()
        ).scalars().all()
    return {code for code in rows if code}


def send_requests(*, pause=(20, 45), sleep=time.sleep) -> dict:
    """Post the request in every vendor group. Returns what happened."""
    from backend.app.integrations.whatsapp import whapi
    from core.time_utils import now_ist_naive

    if not enabled():
        return {"sent": [], "skipped": [], "failed": []}
    evening = now_ist_naive().hour >= 12
    skip_codes = _recent_senders(float(_env("WHAPI_STOCK_REQUEST_SKIP_HOURS", "2")))
    sent, skipped, failed = [], [], []
    # The linked number may not be in every vendor's group: those are left out.
    member_of = {g.get("id") for g in whapi.list_groups()}
    for group, code in whapi.vendor_group_codes().items():
        if member_of and group not in member_of:
            logger.info("Stock request: the linked number is not in %s (%s) -- skipped.", group, code)
            skipped.append(group)
            continue
        if code in skip_codes:
            skipped.append(group)
            continue
        if sent or failed:
            sleep(random.uniform(*pause))
        text = random.choice(EVENING if evening else MORNING)
        try:
            whapi.post_as_owner(group, text)
            sent.append(group)
        except Exception:  # noqa: BLE001 -- one group must not stop the rest
            logger.exception("Stock request: could not post in %s (%s).", group, code)
            failed.append(group)
    logger.info("Group stock request: %d sent, %d skipped (sent stock recently), %d failed.", len(sent), len(skipped), len(failed))
    whapi.record("", "stock request", None,
                 f"{'Evening' if evening else 'Morning'} stock request: asked in {len(sent)} vendor groups, "
                 f"{len(skipped)} skipped (sent stock already / not a member), {len(failed)} failed")
    return {"sent": sent, "skipped": skipped, "failed": failed}


def send_requests_safe() -> None:
    try:
        send_requests()
    except Exception:  # noqa: BLE001 -- a scheduler job must never raise
        logger.exception("Group stock request failed.")
