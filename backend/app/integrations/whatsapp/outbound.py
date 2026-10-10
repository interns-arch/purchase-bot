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


UPDATE_TEMPLATE = "purchase_bot_update"
_WINDOW_HOURS = 23.5  # a little inside WhatsApp's 24 h customer-service window


def in_service_window(to: str) -> bool:
    """True when `to` has written to the bot within the last ~24 hours, so a
    plain message is allowed. False otherwise -- WhatsApp would refuse it
    with 131047 (found live 10 Oct 2026: 16 messages to Prateek sir failed in
    one day). Unknown -> True (old behaviour)."""
    try:
        from datetime import timedelta

        from sqlalchemy import func, select

        from backend.app.integrations.whatsapp.models import WhatsAppChatMessage
        from backend.app.integrations.whatsapp.registry import normalize_number
        from core.db import get_session
        from core.time_utils import now_ist_naive

        with get_session() as session:
            last_in = session.execute(
                select(func.max(WhatsAppChatMessage.created_at)).where(
                    WhatsAppChatMessage.whatsapp_number == normalize_number(to),
                    WhatsAppChatMessage.direction == "in",
                )
            ).scalar()
        return last_in is not None and now_ist_naive() - last_in < timedelta(hours=_WINDOW_HOURS)
    except Exception:  # noqa: BLE001
        logger.exception("Could not check the WhatsApp window for %s.", to)
        return True


def template_approved(name: str) -> bool:
    try:
        from backend.app.integrations.whatsapp.daily_stock import _meta_templates

        return _meta_templates().get(name, ("", 0))[0] == "APPROVED"
    except Exception:  # noqa: BLE001
        return False


def flatten_for_template(text: str, limit: int = 900) -> str:
    """A template parameter may not contain new lines, tabs or runs of spaces."""
    import re

    flat = " | ".join(part.strip(" •") for part in (text or "").splitlines() if part.strip())
    flat = re.sub(r"\s{2,}", " ", flat.replace("\t", " "))
    return flat if len(flat) <= limit else flat[: limit - 30].rstrip() + " … (more in ProcureHub)"


def send_reply_safe(to: str, body: str) -> bool:
    """Returns True on success, False on any failure (logged, never raised).

    Someone who has not written in the last 24 hours cannot receive a plain
    message, so they get the approved `purchase_bot_update` template carrying
    the same text instead."""
    try:
        client = WhatsAppClient(whatsapp_settings)
        if not in_service_window(to) and template_approved(UPDATE_TEMPLATE):
            client.send_template_message(
                to, UPDATE_TEMPLATE, whatsapp_settings.template_language, [flatten_for_template(body)]
            )
        else:
            client.send_text_message(to, body)
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
