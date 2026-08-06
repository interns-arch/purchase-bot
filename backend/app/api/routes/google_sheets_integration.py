"""Google Sheets Integration Status endpoints -- backs the Google Sheets
panel on the Settings > Integration Status page. Mirrors
`backend/app/api/routes/gmail_integration.py`."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.integrations.google_sheets import status_service
from backend.app.integrations.google_sheets.config import google_sheets_settings
from backend.app.integrations.google_sheets.sync_service import (
    GoogleSheetsNotConfiguredError,
    test_connection as run_test_connection,
)
from backend.app.schemas.integration_status import GoogleSheetsIntegrationStatusOut

router = APIRouter(
    prefix="/api/integrations/google-sheets",
    tags=["integration-status"],
    dependencies=[Depends(get_current_user)],
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_status(db: Session) -> GoogleSheetsIntegrationStatusOut:
    status_row = status_service.get_status_row(db)

    if not google_sheets_settings.is_configured():
        sync_status = "NOT_CONFIGURED"
    elif status_row.last_sync_at is None:
        sync_status = "UNTESTED"
    elif status_row.last_sync_success:
        sync_status = "CONNECTED"
    else:
        sync_status = "FAILED"

    return GoogleSheetsIntegrationStatusOut(
        checked_at=_utcnow(),
        enabled=google_sheets_settings.enabled,
        configured=google_sheets_settings.is_configured(),
        sync_status=sync_status,
        sync_message=status_row.last_sync_message,
        last_sync_at=status_row.last_sync_at,
        last_synced_vendor_name=status_row.last_synced_vendor_name,
    )


@router.get("/status", response_model=GoogleSheetsIntegrationStatusOut)
def get_status(db: Session = Depends(get_db)) -> GoogleSheetsIntegrationStatusOut:
    return _build_status(db)


@router.post("/test-connection", response_model=GoogleSheetsIntegrationStatusOut)
def test_connection(db: Session = Depends(get_db)) -> GoogleSheetsIntegrationStatusOut:
    try:
        title = run_test_connection()
        status_service.record_sync(db, success=True, message=f"Connected -- opened '{title}'.")
    except GoogleSheetsNotConfiguredError as exc:
        status_service.record_sync(db, success=False, message=str(exc))
    except Exception as exc:  # noqa: BLE001 -- any failure just gets reported, not raised
        status_service.record_sync(db, success=False, message=str(exc))

    return _build_status(db)
