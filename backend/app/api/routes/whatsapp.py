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
from backend.app.integrations.whatsapp import status_service
from backend.app.integrations.whatsapp.config import whatsapp_settings
from backend.app.integrations.whatsapp.parser import parse_webhook_payload
from backend.app.integrations.whatsapp.webhook import verify_webhook_signature
from backend.app.workers.document_worker import handle_incoming_whatsapp_message
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

    messages = parse_webhook_payload(payload)
    for message in messages:
        background_tasks.add_task(handle_incoming_whatsapp_message, message)

    logger.info("WhatsApp webhook received %d document attachment(s).", len(messages))
    return JSONResponse({"status": "received", "message_count": len(messages)})
