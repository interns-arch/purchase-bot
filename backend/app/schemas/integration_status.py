from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class LastWhatsAppEventOut(BaseModel):
    sender: str | None
    filename: str
    occurred_at: datetime


class WhatsAppIntegrationStatusOut(BaseModel):
    checked_at: datetime
    enabled: bool
    api_health: str

    connection_status: str  # NOT_CONFIGURED | UNTESTED | CONNECTED | FAILED
    connection_message: str | None
    last_connection_tested_at: datetime | None

    webhook_verification_status: str  # NOT_VERIFIED | VERIFIED
    last_webhook_verified_at: datetime | None
    webhook_url: str | None

    access_token_configured: bool
    access_token_status: str  # NOT_CONFIGURED | UNTESTED | VALID | INVALID

    last_incoming_message: LastWhatsAppEventOut | None
    last_downloaded_attachment: LastWhatsAppEventOut | None
    media_download_status: str  # NO_ATTEMPTS_YET | SUCCESS | FAILED
    media_download_message: str | None
