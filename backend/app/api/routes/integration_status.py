"""Integration Status endpoints -- backs the Settings > Integration Status
page. Purely reports state; the only side-effecting action is
`POST /test-connection`, which makes one live Graph API call to confirm the
configured access token + phone number id actually work."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.documents import service as documents_service
from backend.app.documents.models import DocumentSource, IncomingDocumentStatus
from backend.app.integrations.whatsapp import status_service
from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.schemas.integration_status import LastWhatsAppEventOut, WhatsAppIntegrationStatusOut

router = APIRouter(
    prefix="/api/integrations/whatsapp",
    tags=["integration-status"],
    dependencies=[Depends(get_current_user)],
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _build_status(db: Session) -> WhatsAppIntegrationStatusOut:
    status_row = status_service.get_status_row(db)

    access_token_configured = bool(whatsapp_settings.access_token)
    if not access_token_configured:
        access_token_status = "NOT_CONFIGURED"
        connection_status = "NOT_CONFIGURED"
    elif status_row.last_connection_tested_at is None:
        access_token_status = "UNTESTED"
        connection_status = "UNTESTED"
    elif status_row.last_connection_success:
        access_token_status = "VALID"
        connection_status = "CONNECTED"
    else:
        access_token_status = "INVALID"
        connection_status = "FAILED"

    webhook_verification_status = "VERIFIED" if status_row.last_webhook_verified_at else "NOT_VERIFIED"

    recent_whatsapp_docs = documents_service.list_incoming_documents(
        db, source=DocumentSource.WHATSAPP, limit=20
    )

    last_incoming_message = None
    if recent_whatsapp_docs:
        newest = recent_whatsapp_docs[0]
        last_incoming_message = LastWhatsAppEventOut(
            sender=newest.sender, filename=newest.filename, occurred_at=newest.received_at
        )

    last_downloaded = next(
        (d for d in recent_whatsapp_docs if d.status != IncomingDocumentStatus.DOWNLOAD_FAILED),
        None,
    )
    last_downloaded_attachment = (
        LastWhatsAppEventOut(
            sender=last_downloaded.sender,
            filename=last_downloaded.filename,
            occurred_at=last_downloaded.received_at,
        )
        if last_downloaded
        else None
    )

    if not recent_whatsapp_docs:
        media_download_status = "NO_ATTEMPTS_YET"
        media_download_message = None
    elif recent_whatsapp_docs[0].status == IncomingDocumentStatus.DOWNLOAD_FAILED:
        media_download_status = "FAILED"
        media_download_message = recent_whatsapp_docs[0].error_message
    else:
        media_download_status = "SUCCESS"
        media_download_message = None

    return WhatsAppIntegrationStatusOut(
        checked_at=_utcnow(),
        enabled=whatsapp_settings.enabled,
        api_health="OK",
        connection_status=connection_status,
        connection_message=status_row.last_connection_message,
        last_connection_tested_at=status_row.last_connection_tested_at,
        webhook_verification_status=webhook_verification_status,
        last_webhook_verified_at=status_row.last_webhook_verified_at,
        webhook_url=whatsapp_settings.webhook_callback_url,
        access_token_configured=access_token_configured,
        access_token_status=access_token_status,
        last_incoming_message=last_incoming_message,
        last_downloaded_attachment=last_downloaded_attachment,
        media_download_status=media_download_status,
        media_download_message=media_download_message,
    )


@router.get("/status", response_model=WhatsAppIntegrationStatusOut)
def get_status(db: Session = Depends(get_db)) -> WhatsAppIntegrationStatusOut:
    return _build_status(db)


@router.post("/test-connection", response_model=WhatsAppIntegrationStatusOut)
def test_connection(db: Session = Depends(get_db)) -> WhatsAppIntegrationStatusOut:
    if not whatsapp_settings.access_token or not whatsapp_settings.phone_number_id:
        status_service.record_connection_test(
            db,
            success=False,
            message="WHATSAPP_ACCESS_TOKEN and/or WHATSAPP_PHONE_NUMBER_ID are not configured.",
        )
    else:
        client = WhatsAppClient(whatsapp_settings)
        try:
            info = client.get_phone_number_info()
            display_name = info.get("verified_name") or info.get("display_phone_number") or "this number"
            status_service.record_connection_test(
                db, success=True, message=f"Connected -- verified as {display_name}."
            )
        except Exception as exc:  # noqa: BLE001 -- any failure just gets reported, not raised
            status_service.record_connection_test(db, success=False, message=str(exc))

    return _build_status(db)
