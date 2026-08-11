"""Per-number vendor-name memory for the WhatsApp grouping window (backed by
`WhatsAppVendorMemory`). Mirrors `command_store`'s shape.

When a sender supplies a vendor name (file caption or follow-up text), it is
remembered here; further vendor files from the same number within
`WHATSAPP_GROUPING_WINDOW_MINUTES` are grouped under that vendor
automatically instead of asking again. A stale memory (older than the
window) is treated as absent -- the next file starts a fresh conversation."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.models import WhatsAppVendorMemory


def _utcnow() -> datetime:  # patched in tests
    return datetime.utcnow()


def remember(whatsapp_number: str, vendor_name: str, session: Session) -> None:
    row = session.execute(
        select(WhatsAppVendorMemory).where(
            WhatsAppVendorMemory.whatsapp_number == whatsapp_number
        )
    ).scalar_one_or_none()
    if row is None:
        session.add(
            WhatsAppVendorMemory(whatsapp_number=whatsapp_number, vendor_name=vendor_name)
        )
    else:
        row.vendor_name = vendor_name
        row.updated_at = _utcnow()  # explicit: onupdate only fires on changed rows
    session.flush()


def recall(whatsapp_number: str, max_age_minutes: float, session: Session) -> str | None:
    """The vendor name this number supplied within the last `max_age_minutes`,
    or None (no memory / memory expired). Never deletes -- an expired row is
    simply ignored and will be overwritten by the next `remember`."""
    if max_age_minutes <= 0:
        return None
    row = session.execute(
        select(WhatsAppVendorMemory).where(
            WhatsAppVendorMemory.whatsapp_number == whatsapp_number
        )
    ).scalar_one_or_none()
    if row is None or row.updated_at is None:
        return None
    if _utcnow() - row.updated_at > timedelta(minutes=max_age_minutes):
        return None
    return row.vendor_name
