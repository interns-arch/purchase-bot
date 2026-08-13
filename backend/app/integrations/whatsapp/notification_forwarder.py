"""Mirrors every UI toast notification to the Founder's WhatsApp number.

The web UI shows transient toasts for every integration event (import
results, workbook/Sheet updates, allocation reports, Gmail poll failures --
see `backend/app/notifications/broker.py`). This module registers a
forwarder on that broker so the SAME events also arrive as WhatsApp text
messages on `WHATSAPP_ADMIN_PHONE_NUMBER` -- the Founder sees everything
without keeping the web UI open.

Failure-isolated end to end: each send runs on its own daemon thread via
`send_reply_safe` (which never raises), and a disabled/unconfigured setup
simply does nothing. This module never calls `broker.publish` -- a forwarder
that published would loop.
"""

from __future__ import annotations

import threading

from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.outbound import send_reply_safe
from backend.app.notifications import broker
from core.logging_setup import get_logger

logger = get_logger(__name__)

_EMOJI = {"success": "✅", "error": "❌", "warning": "⚠️", "info": "ℹ️"}


def format_notification(note: broker.Notification) -> str:
    """`<emoji> <title>` with the toast's detail lines underneath -- the same
    content the UI toast shows, readable as a plain WhatsApp text."""
    text = f"{_EMOJI.get(note.level, _EMOJI['info'])} {note.title}"
    if note.message:
        text += f"\n{note.message}"
    return text


def forward_notification(note: broker.Notification) -> None:
    """Broker forwarder: mirror one toast to WhatsApp (fire-and-forget).
    Every configured admin number receives it. Events flagged `mirror=False`
    (e.g. "the workbook was sent to WhatsApp" -- the file is already in the
    chat) stay web-only, so the WhatsApp thread stays readable."""
    if not whatsapp_settings.forward_notifications or not note.mirror:
        return
    recipients = whatsapp_settings.admin_phone_numbers
    if not recipients:
        return
    body = format_notification(note)

    def _send_all() -> None:
        for to in recipients:
            send_reply_safe(to, body)

    # Own thread: broker.publish is called from import workers mid-flow; an
    # HTTP round-trip to the Graph API must not slow those down.
    threading.Thread(target=_send_all, daemon=True).start()


def register() -> None:
    """Hook the mirror into the notification broker (idempotent; called once
    at app startup). Registered even when currently unconfigured -- the
    enabled/number checks run per-notification, so flipping the env vars
    takes effect on restart without touching this wiring."""
    broker.add_forwarder(forward_notification)
    logger.info(
        "WhatsApp notification mirror registered (enabled=%s, admin number %s).",
        whatsapp_settings.forward_notifications,
        "set" if whatsapp_settings.admin_phone_number else "NOT set",
    )
