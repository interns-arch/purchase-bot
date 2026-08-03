"""Downloads a WhatsApp media attachment and stages it exactly the way a
manual upload would be staged -- the Document Processing Engine never knows
the difference."""

from __future__ import annotations

from pathlib import Path

from backend.app.documents.models import DocumentSource
from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.services.document_processor import staging


def download_document_media(media_id: str, filename_hint: str, client: WhatsAppClient) -> Path:
    media_meta = client.get_media_url(media_id)
    content = client.download_media(media_meta["url"])
    return staging.save_incoming_bytes(content, filename_hint, DocumentSource.WHATSAPP)
