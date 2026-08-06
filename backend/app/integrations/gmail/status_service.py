"""Reads/writes the single `GmailIntegrationStatus` row -- pure persistence,
no IMAP/Gmail API calls here (see `backend/app/workers/email_worker.py` for
the poll itself). Mirrors `whatsapp/status_service.py`."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.app.integrations.gmail.models import STATUS_ROW_ID, GmailIntegrationStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_status_row(session: Session) -> GmailIntegrationStatus:
    row = session.get(GmailIntegrationStatus, STATUS_ROW_ID)
    if row is None:
        row = GmailIntegrationStatus(id=STATUS_ROW_ID)
        session.add(row)
        session.flush()
    return row


def record_poll(session: Session, *, success: bool, message: str | None) -> GmailIntegrationStatus:
    row = get_status_row(session)
    row.last_poll_at = _utcnow()
    row.last_poll_success = success
    row.last_poll_message = message
    session.flush()
    return row


def record_message_processed(session: Session) -> GmailIntegrationStatus:
    row = get_status_row(session)
    row.last_message_processed_at = _utcnow()
    session.flush()
    return row
