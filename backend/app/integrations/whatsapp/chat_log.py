"""The permanent record of every WhatsApp message on the line -- the data
behind the Chats page.

- Incoming texts / files / photos are logged by the webhook (`log_incoming`).
- Everything the bot sends is logged by `WhatsAppClient` itself
  (`log_outgoing`), so no sender anywhere in the code can be missed.
- WhatsApp's delivery reports move each sent message to delivered / read /
  failed (`update_status`). Reports for messages sent by the other Meta apps
  on the same number are ignored (no matching row).

Logging can never break messaging: every function swallows its own errors."""

from __future__ import annotations

from sqlalchemy import select

from core.logging_setup import get_logger

logger = get_logger(__name__)

_STATUS_RANK = {"failed": 0, "sent": 1, "delivered": 2, "read": 3}


def _number(raw: str | None) -> str:
    from backend.app.integrations.whatsapp.registry import normalize_number

    return normalize_number(raw) or (raw or "")


def log_incoming(
    number: str, *, kind: str, text: str | None = None, filename: str | None = None,
    media_id: str | None = None, wamid: str | None = None,
) -> None:
    try:
        from backend.app.integrations.whatsapp.models import WhatsAppChatMessage
        from core.db import get_session

        with get_session() as session:
            if wamid and session.execute(
                select(WhatsAppChatMessage.id).where(WhatsAppChatMessage.wamid == wamid)
            ).first():
                return  # Meta re-delivered the same webhook
            session.add(
                WhatsAppChatMessage(
                    whatsapp_number=_number(number), direction="in", kind=kind,
                    text=(text or None), filename=filename, media_id=media_id,
                    wamid=wamid, status="received",
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("Could not log an incoming WhatsApp message from %s.", number)


def log_outgoing(
    number: str, *, kind: str, text: str | None = None, filename: str | None = None,
    wamid: str | None = None, error: str | None = None,
) -> None:
    try:
        from backend.app.integrations.whatsapp.models import WhatsAppChatMessage
        from core.db import get_session

        with get_session() as session:
            session.add(
                WhatsAppChatMessage(
                    whatsapp_number=_number(number), direction="out", kind=kind,
                    text=(text or None), filename=filename, wamid=wamid,
                    status="failed" if error else "sent", error=(error or None),
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("Could not log an outgoing WhatsApp message to %s.", number)


_template_bodies: dict[str, str] | None = None
_template_loaded_at = 0.0


def template_text(name: str, params: list[str], client=None) -> str:
    """A template message as the person sees it: the approved body (fetched
    from Meta once, then cached) with {{1}}, {{2}} ... filled in. Falls back
    to '[template name]' when the body is not available."""
    global _template_bodies, _template_loaded_at
    import time

    if _template_bodies is not None and name not in _template_bodies and time.time() - _template_loaded_at > 600:
        _template_bodies = None  # a template approved since the last load
    if _template_bodies is None:
        _template_loaded_at = time.time()
        _template_bodies = {}
        try:
            import os

            import httpx

            waba = os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip()
            if waba and client is not None:
                settings = client._settings
                response = httpx.get(
                    f"{settings.graph_api_base_url}/{waba}/message_templates",
                    params={"fields": "name,components", "limit": 200},
                    headers={"Authorization": f"Bearer {settings.access_token}"},
                    timeout=15,
                )
                for item in response.json().get("data", []):
                    for component in item.get("components", []):
                        if component.get("type") == "BODY" and component.get("text"):
                            _template_bodies[item["name"]] = component["text"]
        except Exception:  # noqa: BLE001 -- a label is enough
            logger.exception("Could not load WhatsApp template texts.")
    body = _template_bodies.get(name)
    if not body:
        return f"[template {name}]" + (f" {' | '.join(params)}" if params else "")
    for index, value in enumerate(params, start=1):
        body = body.replace("{{%d}}" % index, str(value))
    return body


def update_status(wamid: str, status: str, error: str | None = None) -> None:
    """Apply a delivery report. Never moves a message backwards (a late
    'delivered' after 'read' is ignored); 'failed' always wins."""
    if not wamid or status not in _STATUS_RANK:
        return
    try:
        from backend.app.integrations.whatsapp.models import WhatsAppChatMessage
        from core.db import get_session

        with get_session() as session:
            row = session.execute(
                select(WhatsAppChatMessage).where(
                    WhatsAppChatMessage.wamid == wamid, WhatsAppChatMessage.direction == "out"
                )
            ).scalars().first()
            if row is None:
                return
            if status == "failed" or _STATUS_RANK.get(status, 0) > _STATUS_RANK.get(row.status, 0):
                row.status = status
                if error:
                    row.error = error
    except Exception:  # noqa: BLE001
        logger.exception("Could not apply WhatsApp status %s for %s.", status, wamid)
