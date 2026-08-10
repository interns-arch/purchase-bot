"""Store for Vendor Inventory files held while waiting for their vendor
name (backed by `WhatsAppPendingVendorFile`). Mirrors `command_store`'s
shape: tiny, session-in / rows-out, no business logic.

Vendor identity is supplied by the SENDER (caption or follow-up text) and
resolved against the existing Vendor master -- the filename is audit
metadata only and never determines the vendor."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.models import WhatsAppPendingVendorFile


def add(whatsapp_number: str, staged_path: str, original_filename: str, session: Session) -> None:
    session.add(
        WhatsAppPendingVendorFile(
            whatsapp_number=whatsapp_number,
            staged_path=staged_path,
            original_filename=original_filename,
        )
    )
    session.flush()


def list_for(whatsapp_number: str, session: Session) -> list[WhatsAppPendingVendorFile]:
    """This number's held files, oldest first (import order)."""
    return list(
        session.execute(
            select(WhatsAppPendingVendorFile)
            .where(WhatsAppPendingVendorFile.whatsapp_number == whatsapp_number)
            .order_by(WhatsAppPendingVendorFile.id)
        ).scalars()
    )


def remove(row_id: int, session: Session) -> None:
    row = session.get(WhatsAppPendingVendorFile, row_id)
    if row is not None:
        session.delete(row)
        session.flush()
