"""One-off (6 Oct 2026): fill the Chats page with the history the bot already
had before every message was logged -- the files people sent (incoming
documents) and the AI conversations. Safe to run twice (duplicates skipped).

    python -m backend.scripts.backfill_chat_log
"""

from __future__ import annotations

from sqlalchemy import select

from backend.app.documents.models import IncomingDocument
from backend.app.integrations.whatsapp.models import AiConversationMessage, WhatsAppChatMessage
from backend.app.integrations.whatsapp.registry import normalize_number
from core.db import get_session


def main() -> None:
    added_docs = added_ai = 0
    with get_session() as session:
        known_wamids = set(session.execute(select(WhatsAppChatMessage.wamid).where(WhatsAppChatMessage.wamid.is_not(None))).scalars())
        for doc in session.execute(
            select(IncomingDocument).where(IncomingDocument.whatsapp_message_id.is_not(None), IncomingDocument.sender.is_not(None))
        ).scalars():
            if doc.whatsapp_message_id in known_wamids:
                continue
            lower = (doc.filename or "").lower()
            session.add(
                WhatsAppChatMessage(
                    whatsapp_number=normalize_number(doc.sender), direction="in",
                    kind="image" if lower.startswith("photo_") or lower.endswith((".jpg", ".jpeg", ".png")) else "document",
                    filename=doc.filename, wamid=doc.whatsapp_message_id, status="received",
                    created_at=doc.received_at,
                )
            )
            known_wamids.add(doc.whatsapp_message_id)
            added_docs += 1

        existing = {
            (r.whatsapp_number, r.direction, (r.text or "")[:200])
            for r in session.execute(select(WhatsAppChatMessage).where(WhatsAppChatMessage.kind == "text")).scalars()
        }
        for msg in session.execute(select(AiConversationMessage).where(AiConversationMessage.role.in_(["in", "out"]))).scalars():
            key = (normalize_number(msg.whatsapp_number), msg.role, (msg.text or "")[:200])
            if key in existing:
                continue
            session.add(
                WhatsAppChatMessage(
                    whatsapp_number=key[0], direction=msg.role, kind="text", text=msg.text,
                    status="received" if msg.role == "in" else "sent", created_at=msg.created_at,
                )
            )
            existing.add(key)
            added_ai += 1
    print(f"Backfilled {added_docs} received file(s) and {added_ai} AI chat message(s).")


if __name__ == "__main__":
    main()
