"""Whapi.Cloud webhook: the bot's WhatsApp GROUPS (see
`backend.app.integrations.whatsapp.whapi`). Whapi calls this, not a browser,
so security is the shared secret it sends in the X-Whapi-Secret header.

Same hand-off as the Meta webhook: parse -> BackgroundTasks -> the one
document worker, so a group message is handled exactly like a direct one."""

from __future__ import annotations

import hmac
import json

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.responses import JSONResponse

from backend.app.integrations.whatsapp import chat_log, whapi
from backend.app.workers.document_worker import (
    handle_incoming_whatsapp_message,
    handle_incoming_whatsapp_text,
)
from core.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["whatsapp"])


# The second path: the server's public proxy (shared Caddy) forwards only
# /api/whatsapp/webhook* to ProcureHub (found 29 Oct 2026), so Whapi is
# pointed at this one.
@router.post("/api/whapi/webhook")
@router.post("/api/whatsapp/webhook/whapi")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    if not whapi.enabled():
        return JSONResponse({"status": "ignored", "reason": "Whapi integration disabled."})
    secret = whapi.webhook_secret()
    if not secret or not hmac.compare_digest(request.headers.get("X-Whapi-Secret", ""), secret):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid secret.")
    try:
        payload = json.loads(await request.body())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body.") from exc

    parsed_texts, parsed_files = whapi.parse(payload)
    texts = [t for t in parsed_texts if chat_log.first_delivery(t.message_id)]
    files = [f for f in parsed_files if chat_log.first_delivery(f.message_id)]
    for text_message in texts:
        background_tasks.add_task(handle_incoming_whatsapp_text, text_message)
        background_tasks.add_task(
            chat_log.log_incoming, text_message.sender, kind="text",
            text=text_message.text, wamid=text_message.message_id,
        )
    for message in files:
        background_tasks.add_task(handle_incoming_whatsapp_message, message)
        background_tasks.add_task(
            chat_log.log_incoming, message.sender,
            kind="image" if message.is_photo else "document",
            text=message.caption, filename=message.filename,
            media_id=message.media_id, wamid=message.message_id,
        )

    if texts or files:
        logger.info("Whapi webhook: %d group text(s) and %d group file(s) for the bot.", len(texts), len(files))
    return JSONResponse({"status": "received", "text_count": len(texts), "document_count": len(files)})
