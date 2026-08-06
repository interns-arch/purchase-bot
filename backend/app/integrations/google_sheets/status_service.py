"""Reads/writes the single `GoogleSheetsIntegrationStatus` row -- pure
persistence, no Google Sheets API calls here (see `sync_service.py`).
Mirrors `whatsapp/status_service.py`."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.app.integrations.google_sheets.models import (
    STATUS_ROW_ID,
    GoogleSheetsIntegrationStatus,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_status_row(session: Session) -> GoogleSheetsIntegrationStatus:
    row = session.get(GoogleSheetsIntegrationStatus, STATUS_ROW_ID)
    if row is None:
        row = GoogleSheetsIntegrationStatus(id=STATUS_ROW_ID)
        session.add(row)
        session.flush()
    return row


def record_sync(
    session: Session, *, success: bool, message: str | None, vendor_name: str | None = None
) -> GoogleSheetsIntegrationStatus:
    row = get_status_row(session)
    row.last_sync_at = _utcnow()
    row.last_sync_success = success
    row.last_sync_message = message
    if vendor_name is not None:
        row.last_synced_vendor_name = vendor_name
    session.flush()
    return row
