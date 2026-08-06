"""Downloads a WhatsApp media attachment and stages it exactly the way a
manual upload would be staged -- the Document Processing Engine never knows
the difference."""

from __future__ import annotations

from pathlib import Path

from backend.app.documents.models import DocumentSource
from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.services.document_processor import staging
from core.logging_setup import get_logger

logger = get_logger(__name__)


def download_document_media(media_id: str, filename_hint: str, client: WhatsAppClient) -> Path:
    # Step 2: before resolving the media id to a download URL (Graph API call).
    logger.info("WhatsApp media step 2/6: calling get_media_url (media_id=%s)", media_id)
    media_meta = client.get_media_url(media_id)
    # Step 3: get_media_url returned -- log what we got back (URL host only /
    # size / mime, never the token-bearing full URL).
    logger.info(
        "WhatsApp media step 3/6: get_media_url returned (media_id=%s, mime=%s, size=%s bytes)",
        media_id,
        media_meta.get("mime_type"),
        media_meta.get("file_size"),
    )

    # Step 4: before downloading the actual bytes from the short-lived URL.
    logger.info("WhatsApp media step 4/6: calling download_media (media_id=%s)", media_id)
    content = client.download_media(media_meta["url"])
    # Step 5: download_media returned -- log how many bytes came back.
    logger.info(
        "WhatsApp media step 5/6: download_media returned %d bytes (media_id=%s)",
        len(content),
        media_id,
    )

    # Step 6: persist to the staging area exactly like a manual upload.
    saved_path = staging.save_incoming_bytes(content, filename_hint, DocumentSource.WHATSAPP)
    logger.info("WhatsApp media step 6/6: saved file to %s (media_id=%s)", saved_path, media_id)
    return saved_path
