"""WhatsApp Cloud API webhook endpoints. Deliberately has NO
`Depends(get_current_user)` -- Meta calls this, not a logged-in browser
session; security is the verify-token handshake (GET) and HMAC signature
check (POST) instead.

Kept intentionally thin: verify -> parse -> hand off to
`backend.app.workers.document_worker` via `BackgroundTasks` so the response
returns immediately, well within Meta's ack deadline. All the actual
download + processing work (and therefore all business logic) happens
after this returns."""

from __future__ import annotations

import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

from backend.app.database.session import get_db
from backend.app.integrations.whatsapp import chat_log, status_service
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.parser import (
    parse_delivery_statuses,
    parse_text_messages,
    parse_webhook_payload,
)
from backend.app.integrations.whatsapp.webhook import verify_webhook_signature
from backend.app.workers.document_worker import (
    handle_incoming_whatsapp_message,
    handle_incoming_whatsapp_text,
)
from core.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


@router.get("/webhook")
def verify_webhook(request: Request, db: Session = Depends(get_db)) -> PlainTextResponse:
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge", "")

    if mode == "subscribe" and whatsapp_settings.webhook_verify_token and token == whatsapp_settings.webhook_verify_token:
        status_service.record_webhook_verified(db)
        return PlainTextResponse(challenge)

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Verification failed.")


@router.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    if not whatsapp_settings.enabled:
        return JSONResponse({"status": "ignored", "reason": "WhatsApp integration disabled."})

    raw_body = await request.body()

    if whatsapp_settings.app_secret:
        signature = request.headers.get("X-Hub-Signature-256")
        if not verify_webhook_signature(raw_body, signature, whatsapp_settings.app_secret):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature.")

    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body.") from exc

    # Text commands are scheduled before documents so that if a command and a
    # file somehow arrive in the same webhook, the command is stored first.
    texts = parse_text_messages(payload)
    for text_message in texts:
        background_tasks.add_task(handle_incoming_whatsapp_text, text_message)

    documents = parse_webhook_payload(payload)
    for message in documents:
        background_tasks.add_task(handle_incoming_whatsapp_message, message)

    # Delivery reports for messages WE sent. A failed one (e.g. 131047: the
    # person has not written in 24 h) for an advance-order question moves the
    # order on to the next vendor at once.
    for report in parse_delivery_statuses(payload):
        if report.status == "failed" and report.message_id:
            background_tasks.add_task(_handle_delivery_failure, report)
        if report.message_id:
            background_tasks.add_task(
                chat_log.update_status,
                report.message_id,
                report.status,
                f"{report.error_code} {report.error_title}".strip() if report.error_code or report.error_title else None,
            )

    # The Chats page: every incoming text / file / photo, kept permanently.
    for text_message in texts:
        background_tasks.add_task(
            chat_log.log_incoming, text_message.sender, kind="text",
            text=text_message.text, wamid=text_message.message_id,
        )
    for message in documents:
        background_tasks.add_task(
            chat_log.log_incoming, message.sender,
            kind="image" if getattr(message, "is_photo", False) else "document",
            text=message.caption, filename=message.filename,
            media_id=message.media_id, wamid=message.message_id,
        )

    logger.info(
        "WhatsApp webhook received %d text message(s) and %d document attachment(s).",
        len(texts),
        len(documents),
    )
    return JSONResponse(
        {"status": "received", "text_count": len(texts), "document_count": len(documents)}
    )



def _handle_delivery_failure(report) -> None:
    """Never raises: a delivery report must not break the webhook."""
    try:
        from backend.app.advance_orders.config import advance_order_settings
        from core.db import get_session

        logger.warning(
            "WhatsApp could not deliver %s to %s: %s %s",
            report.message_id,
            report.recipient,
            report.error_code,
            report.error_title,
        )
        if not advance_order_settings.enabled:
            return
        from backend.app.advance_orders import service as advance_orders

        with get_session() as session:
            advance_orders.handle_delivery_failure(
                report.message_id, report.recipient, report.error_code, report.error_title, session
            )
    except Exception:  # noqa: BLE001
        logger.exception("Could not handle a WhatsApp delivery report.")
