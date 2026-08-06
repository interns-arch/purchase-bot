"""Gmail Integration Status endpoints -- backs the Gmail panel on the
Settings > Integration Status page. Mirrors
`backend/app/api/routes/integration_status.py`'s WhatsApp endpoints; kept
as a separate router/file since Gmail is its own integration with its own
settings/status model."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.integrations.gmail import status_service
from backend.app.integrations.gmail.client import GmailNotConfiguredError, get_gmail_client
from backend.app.integrations.gmail.config import gmail_settings
from backend.app.schemas.integration_status import GmailIntegrationStatusOut

router = APIRouter(
    prefix="/api/integrations/gmail",
    tags=["integration-status"],
    dependencies=[Depends(get_current_user)],
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_status(db: Session) -> GmailIntegrationStatusOut:
    status_row = status_service.get_status_row(db)

    if not gmail_settings.is_configured():
        poll_status = "NOT_CONFIGURED"
    elif status_row.last_poll_at is None:
        poll_status = "UNTESTED"
    elif status_row.last_poll_success:
        poll_status = "CONNECTED"
    else:
        poll_status = "FAILED"

    return GmailIntegrationStatusOut(
        checked_at=_utcnow(),
        enabled=gmail_settings.enabled,
        auth_mode=gmail_settings.auth_mode,
        configured=gmail_settings.is_configured(),
        poll_status=poll_status,
        poll_message=status_row.last_poll_message,
        last_poll_at=status_row.last_poll_at,
        last_message_processed_at=status_row.last_message_processed_at,
        poll_interval_seconds=gmail_settings.poll_interval_seconds,
    )


@router.get("/status", response_model=GmailIntegrationStatusOut)
def get_status(db: Session = Depends(get_db)) -> GmailIntegrationStatusOut:
    return _build_status(db)


@router.post("/test-connection", response_model=GmailIntegrationStatusOut)
def test_connection(db: Session = Depends(get_db)) -> GmailIntegrationStatusOut:
    try:
        client = get_gmail_client(gmail_settings)
        client.fetch_unread_messages()
        status_service.record_poll(db, success=True, message="Connected -- inbox is reachable.")
    except GmailNotConfiguredError as exc:
        status_service.record_poll(db, success=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001 -- any failure just gets reported, not raised
        status_service.record_poll(db, success=False, message=str(exc))

    return _build_status(db)
