"""Reads/writes the single `WhatsAppIntegrationStatus` row -- pure
persistence, no HTTP/Graph API calls here (see
`backend/app/api/routes/integration_status.py` for the caller that
actually performs the live connection test)."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend.app.integrations.whatsapp.models import STATUS_ROW_ID, WhatsAppIntegrationStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_status_row(session: Session) -> WhatsAppIntegrationStatus:
    row = session.get(WhatsAppIntegrationStatus, STATUS_ROW_ID)
    if row is None:
        row = WhatsAppIntegrationStatus(id=STATUS_ROW_ID)
        session.add(row)
        session.flush()
    return row


def record_webhook_verified(session: Session) -> WhatsAppIntegrationStatus:
    row = get_status_row(session)
    row.last_webhook_verified_at = _utcnow()
    session.flush()
    return row


def record_connection_test(
    session: Session, *, success: bool, message: str | None
) -> WhatsAppIntegrationStatus:
    row = get_status_row(session)
    row.last_connection_tested_at = _utcnow()
    row.last_connection_success = success
    row.last_connection_message = message
    session.flush()
    return row
