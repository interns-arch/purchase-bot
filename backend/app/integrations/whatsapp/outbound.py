"""Best-effort outbound WhatsApp text replies for the command-routing layer.

Only ever sends short routing prompts/confirmations back to the person who
just messaged us (well within WhatsApp's 24h customer-service window) -- it
never sends business documents, purchase orders, or anything to a vendor.

`send_reply_safe` never raises: a reply failure (WhatsApp not configured, a
transient Graph API error) is logged and returns False, so it can never
crash the background worker or block the actual import routing."""

from __future__ import annotations

from backend.app.integrations.whatsapp.client import WhatsAppClient
from backend.app.integrations.whatsapp.config import whatsapp_settings
from core.logging_setup import get_logger

logger = get_logger(__name__)


def send_reply_safe(to: str, body: str) -> bool:
    """Returns True on success, False on any failure (logged, never raised)."""
    try:
        WhatsAppClient(whatsapp_settings).send_text_message(to, body)
        return True
    except Exception:  # noqa: BLE001 -- a reply failure must never crash the worker
        logger.exception("Failed to send WhatsApp reply to %s", to)
        return False


def send_document_safe(
    to: str, content: bytes, filename: str, mime_type: str, caption: str | None = None
) -> bool:
    """Upload a document and send it to `to`. Returns True on success, False on
    any failure (logged, never raised) -- an output-delivery failure must never
    affect the already-committed import. Never logs the access token."""
    try:
        client = WhatsAppClient(whatsapp_settings)
        media_id = client.upload_media(content, filename, mime_type)
        client.send_document_message(to, media_id, filename, caption=caption)
        return True
    except Exception:  # noqa: BLE001 -- a delivery failure must never crash the worker
        logger.exception("Failed to send WhatsApp document %r to %s", filename, to)
        return False
