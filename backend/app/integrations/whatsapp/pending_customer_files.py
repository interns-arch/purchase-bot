"""Store for Customer Order files held while waiting for their CUSTOMER name.

The exact counterpart of `pending_vendor_files`. An UNREGISTERED sender's
Customer Order file takes its customer identity from the name the sender
supplies (file caption, or a follow-up text) -- the filename is audit
metadata only. A REGISTERED customer number never uses this: the number is
the identity.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.models import WhatsAppPendingCustomerFile
from backend.app.integrations.whatsapp.registry import normalize_number
from core.logging_setup import get_logger

logger = get_logger(__name__)


def add(
    whatsapp_number: str, staged_path: str, original_filename: str, session: Session
) -> None:
    session.add(
        WhatsAppPendingCustomerFile(
            whatsapp_number=normalize_number(whatsapp_number),
            staged_path=staged_path,
            original_filename=original_filename,
        )
    )
    session.flush()


def list_for(whatsapp_number: str, session: Session) -> list[WhatsAppPendingCustomerFile]:
    """This number's held files, oldest first (import order)."""
    return list(
        session.execute(
            select(WhatsAppPendingCustomerFile)
            .where(
                WhatsAppPendingCustomerFile.whatsapp_number
                == normalize_number(whatsapp_number)
            )
            .order_by(WhatsAppPendingCustomerFile.created_at, WhatsAppPendingCustomerFile.id)
        ).scalars()
    )


def remove(row_id: int, session: Session) -> None:
    row = session.get(WhatsAppPendingCustomerFile, row_id)
    if row is not None:
        session.delete(row)
        session.flush()
