from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from backend.app.schemas.types import IstDateTime


class LastWhatsAppEventOut(BaseModel):
    sender: str | None
    filename: str
    occurred_at: IstDateTime


class WhatsAppIntegrationStatusOut(BaseModel):
    checked_at: IstDateTime
    enabled: bool
    api_health: str

    connection_status: str  # NOT_CONFIGURED | UNTESTED | CONNECTED | FAILED
    connection_message: str | None
    last_connection_tested_at: IstDateTime | None

    webhook_verification_status: str  # NOT_VERIFIED | VERIFIED
    last_webhook_verified_at: IstDateTime | None
    webhook_url: str | None

    access_token_configured: bool
    access_token_status: str  # NOT_CONFIGURED | UNTESTED | VALID | INVALID

    last_incoming_message: LastWhatsAppEventOut | None
    last_downloaded_attachment: LastWhatsAppEventOut | None
    media_download_status: str  # NO_ATTEMPTS_YET | SUCCESS | FAILED
    media_download_message: str | None


class GmailIntegrationStatusOut(BaseModel):
    checked_at: IstDateTime
    enabled: bool
    auth_mode: str  # imap | oauth
    configured: bool

    poll_status: str  # NOT_CONFIGURED | UNTESTED | CONNECTED | FAILED
    poll_message: str | None
    last_poll_at: IstDateTime | None
    last_message_processed_at: IstDateTime | None
    poll_interval_seconds: int


class GoogleSheetsIntegrationStatusOut(BaseModel):
    checked_at: IstDateTime
    enabled: bool
    configured: bool

    sync_status: str  # NOT_CONFIGURED | UNTESTED | CONNECTED | FAILED
    sync_message: str | None
    last_sync_at: IstDateTime | None
    last_synced_vendor_name: str | None
